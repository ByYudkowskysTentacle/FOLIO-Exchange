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
    assert win.startswith('schtasks /Create /TN "FOLIO-Exchange (main)" /SC HOURLY')
    assert '/RU "LIB\\fx" /RP' in win and "run --due" in win
    cron = s.os_command(Path("/srv/site"), "/opt/fx/bin/folio-exchange", "Linux")
    assert cron.startswith("7 * * * * '/opt/fx/bin/folio-exchange' --site ") and "run --due" in cron
    assert "schtasks /Run" in s.os_helpers(Path("C:/sites/main"), "Windows")["run now"]
