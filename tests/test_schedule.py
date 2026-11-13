"""Schedules: parsing, slots across daylight saving, and when a feed is due."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from folio_exchange import schedule as s
from folio_exchange.errors import ConfigError

NY = ZoneInfo("America/New_York")
UTC = timezone.utc


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


@pytest.mark.parametrize("text,kind,days,hm", [
    ("daily", "daily", tuple(range(7)), (2, 0)), ("weekly", "weekly", (6,), (2, 0)),
    ("monthly", "monthly", (1,), (2, 0)), ("daily 03:30", "daily", tuple(range(7)), (3, 30)),
    ("Sun 02:15", "weekly", (6,), (2, 15)), ("mon,THU 06:00", "weekly", (0, 3), (6, 0)),
    ("monthly 15 01:00", "monthly", (15,), (1, 0)), ("manual", "manual", (), (0, 0)),
])
def test_parse(text, kind, days, hm):
    sc = s.parse(text)
    assert (sc.kind, sc.days, (sc.hour, sc.minute)) == (kind, days, hm)


@pytest.mark.parametrize("text", ["", "hourly", "Sun 25:00", "monthly 31 01:00", "Funday 02:00", "daily 3pm"])
def test_parse_rejects(text):
    with pytest.raises(ConfigError, match="Use daily"):
        s.parse(text)


def test_describe():
    assert s.describe(s.parse("Mon,Thu 06:00")) == "Mondays and Thursdays at 06:00"
    assert s.describe(s.parse("manual")) == "only when started by hand"


def test_slots_in_a_time_zone():
    sc = s.parse("Sun 02:15")
    now = at("2026-10-28T12:00:00+00:00")                            # a Wednesday
    assert s.last_slot(sc, now, NY) == at("2026-10-25T06:15:00+00:00")
    assert s.next_slot(sc, now, NY) == at("2026-11-01T07:15:00+00:00")  # EST from 1 Nov
    assert s.last_slot(s.parse("manual"), now, NY) is None


def test_spring_forward_moves_a_missing_time_later_and_fall_back_runs_once():
    sc = s.parse("Sun 02:15")                                        # 02:15 does not exist on 8 Mar 2026
    assert s.last_slot(sc, at("2026-03-08T12:00:00+00:00"), NY) == at("2026-03-08T07:15:00+00:00")
    one = s.parse("Sun 01:30")                                       # 01:30 happens twice on 1 Nov 2026
    slots = {s.last_slot(one, at("2026-11-01T05:40:00+00:00") + timedelta(minutes=m), NY) for m in range(0, 180, 10)}
    assert slots == {at("2026-11-01T05:30:00+00:00")}                # the first 01:30, once


def run(started: str, status: str, trigger: str = "schedule") -> dict:
    return {"started": started, "status": status, "trigger": trigger}


WEEKLY = s.parse("Sun 02:15")
NOW = at("2026-10-27T09:00:00+00:00")                                 # Tuesday; last slot Sun 25 Oct 02:15 UTC
SLOT = "2026-10-25T02:15:00+00:00"
LATER = "2026-10-26T09:00:00+00:00"


def due(runs, created=None, attempts=3):
    return s.is_due(WEEKLY, NOW, UTC, runs, attempts, created)


def test_due_after_a_missed_slot_and_not_after_success():
    assert due([])
    assert not due([run(LATER, "success")])
    assert not due([run(LATER, "held")]) and not due([run(LATER, "blocked")])


def test_failed_scheduled_runs_retry_up_to_the_limit():
    assert due([run(LATER, "failed")] * 2)
    assert not due([run(LATER, "failed")] * 3)


def hourly_passes(text: str, late: timedelta, off=lambda when: False, max_attempts: int = 6):
    """Simulate the hourly task (minute 7) through one slot of a feed whose every attempt fails, each recorded
    ``late`` after its pass started (as a run is, once its sync has timed out). Returns the passes that made an
    attempt, and the pass after which the slot counts as given up (None: not before the next slot)."""
    sc = s.parse(text)
    slot = at("2026-10-25T02:00:00+00:00")
    nxt = s.next_slot(sc, slot, UTC)
    runs, attempts, given = [], [], None
    p = slot + timedelta(minutes=7)
    while p < nxt:
        if not off(p):
            if s.is_due(sc, p, UTC, runs, max_attempts, None):
                runs.append(run((p + late).isoformat(), "failed"))
                attempts.append(p)
            if runs and given is None and s.out_of_attempts(runs, max_attempts, s.next_slot(sc, p, UTC)):
                given = p
        p += timedelta(hours=1)
    return [a.strftime("%d %H:%M") for a in attempts], given and given.strftime("%d %H:%M")


def test_retries_come_1_2_4_then_8_hours_on_the_hourly_starts_however_long_an_attempt_took():
    """Regression (review of 12 Nov): a gap measured from when the run was recorded, after a 13-minute sync timeout,
    pushed every retry an hour late, so a daily feed ran out of time before its last attempt and its given-up
    alert never came."""
    for late in (timedelta(minutes=2), timedelta(minutes=13), timedelta(minutes=50)):
        attempts, given = hourly_passes("daily 02:00", late)
        assert attempts == ["25 02:07", "25 03:07", "25 05:07", "25 09:07", "25 17:07", "26 01:07"], late
        assert given == "26 01:07"                                  # given up within the slot, after the 6th


def test_attempts_over_an_hour_long_move_their_retries_but_the_slot_is_still_given_up_inside_it():
    """A run recorded in the clock hour after its pass started counts its gap from that hour, so its retries come
    an hour later. Fewer attempts fit, and the slot is given up after the last that does."""
    attempts, given = hourly_passes("daily 02:00", timedelta(minutes=55))
    assert attempts == ["25 02:07", "25 04:07", "25 07:07", "25 12:07", "25 21:07"]
    assert given == "25 21:07"


def test_a_slot_that_starts_late_is_given_up_when_no_retry_fits_before_the_next():
    """The PC was off until 19:00: three attempts fit before 02:00, and the slot is given up after the third."""
    attempts, given = hourly_passes("daily 02:00", timedelta(minutes=13),
                                    off=lambda when: when < at("2026-10-25T19:00:00+00:00"))
    assert attempts == ["25 19:07", "25 20:07", "25 22:07"] and given == "25 22:07"


def test_a_slot_cut_short_by_the_pc_being_off_is_not_given_up_inside_it():
    """Off from 19:00: the slot ends with attempts left, so the next slot's first pass reports it
    (runner.missed_slot, tested with a real site)."""
    attempts, given = hourly_passes("daily 02:00", timedelta(minutes=13),
                                    off=lambda when: when >= at("2026-10-25T19:00:00+00:00"))
    assert len(attempts) == 5 and given is None


def test_a_weekly_slot_has_all_its_attempts_in_about_a_day():
    attempts, given = hourly_passes("Sun 02:00", timedelta(minutes=13))
    assert len(attempts) == 6 and given == attempts[-1] == "26 01:07"


def test_a_retry_one_hour_on_is_not_pushed_back_by_the_runs_own_minutes():
    """The task starts at minute 7: an attempt recorded at 03:20 (its pass began at 03:07) is retried at 04:07."""
    slot = at("2026-10-25T03:00:00+00:00")
    daily = s.parse("daily 03:00")
    runs = [run("2026-10-25T03:20:00+00:00", "failed")]
    assert s.next_retry(runs) == at("2026-10-25T03:50:00+00:00")
    assert s.is_due(daily, at("2026-10-25T04:07:01+00:00"), UTC, runs, 6, None)
    assert not s.is_due(daily, at("2026-10-25T03:45:00+00:00"), UTC, runs, 6, None)
    assert s.last_slot(daily, at("2026-10-25T04:07:01+00:00"), UTC) == slot


def test_attempts_run_out_or_stop_after_a_refused_login():
    one = [run(LATER, "failed")]
    assert not s.out_of_attempts(one, 2)
    assert s.out_of_attempts(one * 2, 2)
    refused = [{**run(LATER, "failed"), "report": {"no_retry": True}}]
    assert s.out_of_attempts(refused, 6)


def test_runs_by_hand_do_not_cancel_the_scheduled_delivery():
    assert due([run(LATER, "built", "manual"), run(LATER, "preview", "preview"), run(LATER, "failed", "manual")])
    assert not due([run(LATER, "success", "manual")])               # delivered by hand: the slot is done


def test_a_new_feed_waits_for_its_first_slot_but_edits_do_not_cancel_a_catch_up():
    assert not due([], created=at("2026-10-26T00:00:00+00:00"))
    assert due([], created=at("2026-10-20T00:00:00+00:00"))
    assert due([], created=None)                                    # has run before: no created rule


def test_manual_feeds_are_never_due():
    assert not s.is_due(s.parse("manual"), NOW, UTC, [], 3, None)


def test_os_commands():
    from pathlib import Path

    win = s.os_command(Path("C:/sites/main"), r"C:\fx\folio-exchange.exe", "Windows", "LIB\\fx")
    assert "Register-ScheduledTask -TaskName 'FOLIO-Exchange (main)'" in win and "run --due" in win
    assert "Get-Credential -UserName 'LIB\\fx'" in win and "-AllowStartIfOnBatteries" in win
    assert "schtasks" not in win
    cron = s.os_command(Path("/srv/site"), "/opt/fx/bin/folio-exchange", "Linux")
    assert cron.startswith("7 * * * * '/opt/fx/bin/folio-exchange' --site ") and "run --due" in cron
    assert "schtasks /Run" in s.os_helpers(Path("C:/sites/main"), "Windows")["run now"]
