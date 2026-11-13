"""The tray's Qt-free parts (D4): the list of sites it watches, its Startup entry, reading a site, and what it
tells the person. They run everywhere, with or without Qt."""
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import REAL_APP_FOLDERS

from folio_exchange.desktop import places, startup, watch
from folio_exchange.history import History


# ------------------------------------------------------------------ the list of sites
def test_sites_are_watched_in_order_once_each_and_can_be_taken_off(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    assert places.watched_sites() == [] and not places.is_watched(a)
    places.watch_site(a, True)
    places.watch_site(b, True)
    places.watch_site(a, True)                                     # again: it moves to the end, once
    assert places.watched_sites() == [b, a]
    assert places.is_watched(a) and places.is_watched(b)
    places.watch_site(b, False)
    assert places.watched_sites() == [a]


def test_a_renamed_site_keeps_its_place_and_others_are_left_alone(tmp_path):
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    places.watch_site(a, True)
    places.watch_site(b, True)
    places.rename_watched(a, c)
    assert places.watched_sites() == [c, b]
    places.rename_watched(tmp_path / "not-watched", tmp_path / "other")
    assert places.watched_sites() == [c, b]


def test_an_unreadable_list_is_empty_and_a_list_that_cannot_be_saved_says_so(tmp_path, monkeypatch):
    places.local_dir().mkdir(parents=True, exist_ok=True)
    (places.local_dir() / places.TRAY_FILE).write_text("{not json", encoding="utf-8")
    assert places.watched_sites() == []
    blocked = tmp_path / "a-file"
    blocked.write_text("")
    monkeypatch.setattr(places, "local_dir", lambda: blocked / "settings")
    with pytest.raises(OSError):
        places.watch_site(tmp_path / "a", True)


def test_the_tray_list_stays_on_this_pc_and_no_site_can_take_its_folder(monkeypatch, tmp_path):
    """The tray's list names folders on this PC, so it's kept where Windows never roams it, and a roaming
    profile never brings another PC's list along."""
    from folio_exchange import siteops

    places.watch_site(tmp_path / "a", True)
    assert (places.local_dir() / places.TRAY_FILE).is_file()
    assert not (places.config_dir() / places.TRAY_FILE).exists()
    real_config_dir, real_local_dir = REAL_APP_FOLDERS             # computed only, never written
    monkeypatch.setattr(places.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\you\AppData\Local")
    monkeypatch.setenv("APPDATA", r"C:\Users\you\AppData\Roaming")
    assert str(real_local_dir()).replace("/", "\\").endswith(r"AppData\Local\FOLIO-Exchange\.app")
    assert "Roaming" in str(real_config_dir())
    assert siteops.name_problem(places.LOCAL_NAME) and siteops.name_problem(".hidden")


def test_one_tray_per_windows_user(monkeypatch):
    monkeypatch.setenv("USERDOMAIN", "LIBRARY")
    mine = places.tray_instance_name()
    assert mine.startswith("folio-exchange-tray-") and "LIBRARY" not in mine
    monkeypatch.setenv("USERDOMAIN", "OTHER")
    assert places.tray_instance_name() != mine


# ------------------------------------------------------------------ the Startup entry
def test_the_tray_starts_by_this_installs_full_path_never_from_the_current_folder(tmp_path, monkeypatch):
    exe, args, folder = startup.tray_command()
    assert args[-1] == "--tray" and Path(folder) == Path(sys.executable).parent
    assert "-m" not in args or "-P" in args or sys.version_info < (3, 11)
    monkeypatch.setattr(startup.sysconfig, "get_path", lambda _name: str(tmp_path))      # no app script
    exe, args, _folder = startup.tray_command()
    assert args == [*(["-P"] if sys.version_info >= (3, 11) else []), "-m", "folio_exchange.desktop", "--tray"]
    assert startup.startup_line() == subprocess.list2cmdline([exe, *args])


def test_the_startup_entry_is_added_and_removed(registry):
    key = (startup.RUN_KEY, startup.RUN_VALUE)
    assert startup.startup_state() == "off"
    startup.set_startup(True)
    assert registry.values[key] == startup.startup_line()
    assert startup.startup_state() == "on"
    startup.set_startup(False)
    assert key not in registry.values and startup.startup_state() == "off"
    startup.set_startup(False)                                     # removing it twice is fine


def test_an_entry_for_another_copy_is_told_apart(registry):
    registry.values[(startup.RUN_KEY, startup.RUN_VALUE)] = r'"C:\Old\folio-exchange-app.exe" --tray'
    assert startup.startup_state() == "other"


def test_a_refused_write_raises_and_off_windows_there_is_no_entry(registry, monkeypatch):
    registry.refuse = PermissionError(5, "Access is denied")
    with pytest.raises(OSError):
        startup.set_startup(True)
    monkeypatch.setattr(startup, "_winreg", lambda: None)
    assert startup.startup_state() is None
    with pytest.raises(RuntimeError, match="Windows only"):
        startup.set_startup(True)


# ------------------------------------------------------------------ reading a site
def add_run(site: Path, status: str, trigger: str = "schedule", error: str | None = None) -> int:
    with History(site / "data" / "history.sqlite") as h:
        run_id = h.start_run("chq", "chq-academic", "1.0.0", trigger, site / "out" / "chq" / f"run-{status}")
        h.finish_run(run_id, status, error=error, report={"held": "first delivery to a test folder"}
                     if status == "held" else {})
    return run_id


def test_a_site_is_read_with_its_waiting_run_and_task(site):
    snap = watch.look(site)
    assert snap["error"] is None and snap["name"] == site.name
    [feed] = snap["feeds"]
    assert feed["name"] == "chq" and feed["last"] is None and feed["waiting"] is None
    held = add_run(site, "held")
    [feed] = watch.look(site)["feeds"]
    assert feed["waiting"]["id"] == held and feed["waiting"]["trigger"] == "schedule"
    assert feed["last"]["status"] == "held"
    assert watch.look(site)["task"]["state"] in ("never", "unneeded", "ok", "stale")


def test_a_build_by_hand_is_not_waiting_for_the_tray(site):
    add_run(site, "built", trigger="build")
    [feed] = watch.look(site)["feeds"]
    assert feed["waiting"] is None                                 # someone asked for it; the tray doesn't nag


def test_a_site_that_has_gone_is_an_error_in_words(tmp_path):
    snap = watch.look(tmp_path / "gone")
    assert snap["error"] and snap["feeds"] == []


# ------------------------------------------------------------------ what is told
def feed(name="chq", *, last=None, waiting=None, overdue=None, enabled=True) -> dict:
    return {"name": name, "title": name, "enabled": enabled, "last": last, "waiting": waiting, "overdue": overdue}


def run(n: int, status: str, trigger="schedule", error=None, repeat=False) -> dict:
    return {"id": n, "status": status, "trigger": trigger, "started": "2026-11-15T03:37:00+00:00", "error": error,
            "not_sent": False, "repeat": repeat}


def snap(*feeds, task=None, error=None, broken=()) -> dict:
    return {"root": "C:/sites/lakeside", "name": "lakeside", "error": error, "feeds": list(feeds),
            "task": task or {"state": "ok", "failing": False}, "broken": list(broken)}


def test_the_first_reading_lists_everything_that_needs_you():
    first = snap(feed("chq", waiting=run(3, "held", "manual"), last=run(3, "held", "manual")),
                 feed("oclc", last=run(5, "failed", "manual")),
                 feed("disc", last=run(6, "success"), overdue="overdue: last success 2026-11-01"),
                 task={"state": "stale", "failing": False}, broken=["bad"])
    assert watch.news(None, first) == [
        "chq is held for your review.", "oclc's last run failed.", "disc hasn't run on schedule.",
        "1 feed file can't be loaded."]
    assert watch.needs(first)[3] == "Scheduled runs have stopped."     # the menu and the count have it
    assert watch.news(None, snap(feed("chq", last=run(1, "success")))) == []


def test_later_readings_tell_only_new_scheduled_runs():
    before = snap(feed(last=run(1, "success")))
    assert watch.news(before, before) == []
    held = snap(feed(last=run(2, "held"), waiting=run(2, "held")))
    assert watch.news(before, held) == ["chq is held for your review."]
    assert watch.news(held, held) == []                            # told once
    blocked = snap(feed(last=run(3, "blocked"), waiting=run(3, "blocked")))
    assert watch.news(before, blocked) == ["chq was blocked. Nothing was sent."]
    by_hand = snap(feed(last=run(4, "held", "manual"), waiting=run(4, "held", "manual")))
    assert watch.news(before, by_hand) == []                       # seen where it was started


def test_a_failed_retry_in_the_same_slot_is_not_told_again_but_a_new_slots_failure_is():
    """Regression (review of 12 Nov): the tray muted any failure with the same error as the last, so Tuesday's
    failure went untold after Monday's. It now follows the alerts' rule (``repeat``, from runner.repeat_failure)."""
    before = snap(feed(last=run(1, "success")))
    failed = snap(feed(last=run(2, "failed", error="Cannot reach FOLIO.")))
    assert watch.news(before, failed) == ["chq's scheduled run failed."]
    retry = snap(feed(last=run(3, "failed", error="Cannot reach FOLIO.", repeat=True)))
    assert watch.news(failed, retry) == []
    next_slot = snap(feed(last=run(4, "failed", error="Cannot reach FOLIO.")))   # same error, new slot
    assert watch.news(retry, next_slot) == ["chq's scheduled run failed."]
    assert watch.news(before, snap(feed(last=run(5, "failed", "manual")))) == []


def test_an_overdue_feed_is_told_when_it_becomes_overdue():
    ok = snap(feed(last=run(1, "success")))
    late = snap(feed(last=run(1, "success"), overdue="overdue"))
    assert watch.news(ok, late) == ["chq hasn't run on schedule."]
    assert watch.news(late, late) == []


def test_the_task_is_told_once_per_problem_and_stopped_waits_while_quiet():
    """Regression (review of 12 Nov): a PC that was off or asleep looks like a stopped task until the next hourly
    start, so the tray said "Scheduled runs have stopped." every morning."""
    stale = snap(task={"state": "stale", "failing": False})
    assert watch.task_news(stale, None, quiet=True) == ([], None)            # just woken: not yet
    assert watch.task_news(stale, None, quiet=False) == (["Scheduled runs have stopped."], "stopped")
    assert watch.task_news(stale, "stopped", quiet=False) == ([], "stopped")  # told once
    failing = snap(task={"state": "stale", "failing": True})
    assert watch.task_news(failing, None, quiet=True) == (["Scheduled runs are failing."], "failing")
    stuck = snap(task={"state": "stuck"})
    assert watch.task_news(stuck, "failing", quiet=True) == (["A run has been going for hours."], "stuck")
    ok = snap(task={"state": "ok"})
    assert watch.task_news(ok, "stuck", quiet=False) == ([], None)           # cleared: told again next time
    assert watch.task_news(snap(error="gone"), None, quiet=False) == ([], None)
    assert watch.count([stale], quiet=True) == 0 and watch.count([stale]) == 1


def test_a_site_that_cannot_be_read_is_told_once_and_again_when_it_comes_back():
    ok = snap(feed(last=run(1, "held"), waiting=run(1, "held")))
    gone = snap(error="The site folder isn't there.")
    assert watch.news(ok, gone) == ["Couldn't read the site. The site folder isn't there."]
    assert watch.news(gone, gone) == []
    assert watch.news(gone, ok) == ["chq is held for your review."]  # back: what still needs you


def test_menu_lines_and_tooltip_in_words():
    assert watch.feed_line(feed(waiting=run(2, "held"), last=run(2, "held"))) == "chq: Held for review"
    assert watch.feed_line(feed(last=run(2, "failed"))) == "chq: Last run failed"
    assert watch.feed_line(feed(enabled=False, last=run(2, "success"))) == "chq: Paused"
    assert watch.feed_line(feed()) == "chq: No runs yet"
    line = watch.feed_line(feed(last=run(2, "success"), overdue="overdue"))
    assert line.startswith("chq: Delivered, ") and line.endswith(", overdue")
    assert watch.tooltip([]) == "FOLIO-Exchange"
    assert watch.tooltip([snap(feed(last=run(1, "success")))]) == "FOLIO-Exchange: nothing needs you right now"
    assert watch.tooltip([snap(feed(waiting=run(2, "held")))]) == "FOLIO-Exchange: 1 thing needs you"
    two = snap(feed(waiting=run(2, "held")), feed("oclc", last=run(3, "failed")))
    assert watch.tooltip([two]) == "FOLIO-Exchange: 2 things need you"


def test_a_real_sites_retry_is_marked_a_repeat_only_within_its_slot(site):
    """look() marks a scheduled failure a repeat as the alerts do: the same error as the attempt before it,
    in the same slot."""
    first = add_run(site, "failed", error="Sync failed: Cannot reach FOLIO.")
    [f] = watch.look(site)["feeds"]
    assert f["last"]["id"] == first and f["last"]["repeat"] is False
    second = add_run(site, "failed", error="Sync failed: Cannot reach FOLIO.")
    [f] = watch.look(site)["feeds"]
    assert f["last"]["id"] == second and f["last"]["repeat"] is True    # both in this week's slot
    with History(site / "data" / "history.sqlite") as h:          # the first attempt was in an earlier slot
        h.db.execute("UPDATE runs SET started = ? WHERE id = ?",
                     ((datetime.now(timezone.utc) - timedelta(days=8)).isoformat(), first))
        h.db.commit()
    [f] = watch.look(site)["feeds"]
    assert f["last"]["repeat"] is False


def test_the_apps_own_folder_never_opens_as_a_site(tmp_path):
    """Regression (review of 12 Nov): the tray's log made its folder look like a site whose setup wasn't
    finished, so the picker would have opened it."""
    (places.local_dir() / "logs").mkdir(parents=True, exist_ok=True)
    (places.local_dir() / "logs" / "folio-exchange.log").write_text("")
    assert not places.unfinished_site(places.local_dir())
    assert "own folder" in places.folder_problem(places.local_dir())


def test_an_overdue_feed_from_a_real_site_reaches_the_menu(site):
    """End to end on a real site: a weekly feed seen weeks ago with no successful run is overdue."""
    with History(site / "data" / "history.sqlite") as h:
        h.set_meta("feed_seen:chq", (datetime.now(timezone.utc) - timedelta(days=30)).isoformat())
    [f] = watch.look(site)["feeds"]
    assert f["overdue"] and watch.feed_line(f).endswith(", overdue")


def test_a_rename_from_the_command_line_or_page_carries_the_tray_list(site):
    """Regression (review of 12 Nov): only the desktop window's rename moved the tray's list along."""
    from sitekit import make_feed

    from folio_exchange import siteops

    make_feed(site, delivery={"transport": "local", "directory": str(site.parent / "vendor-drop")})
    places.watch_site(site.resolve(), True)
    res = siteops.rename_site(site, "Lakeside", via="cli")
    assert places.watched_sites() == [Path(res["root"])]
