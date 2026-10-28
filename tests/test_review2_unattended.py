"""Second review round, "Unattended runs that stop or go quiet" (docs/review-2026-10-27-round-2.md):
LOCK-release-prompt-blocks-schedule, HIST-write-failure-no-alert-heartbeat-green, HIST-crash-on-last-attempt-unreported,
SCHED-outage-exhausts-slot-alerts-lost and LOGIN-404-abandons-slot."""
import json
import os
import sqlite3
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest
from sitekit import make_feed, write_yaml

from folio_exchange import client as client_mod
from folio_exchange import feedops, notify, runner
from folio_exchange.cli import main
from folio_exchange.client import FolioClient, FolioUnreachable, LoginFailed
from folio_exchange.config import load_site, read_yaml
from folio_exchange.errors import FxError
from folio_exchange.history import History
from folio_exchange.lock import RunLock
from folio_exchange.recipe import RecipeLibrary, load_all, load_feeds

can_see_locks = pytest.mark.skipif(sys.platform != "win32" and not os.path.exists("/proc/locks"),
                                   reason="held locks are seen on Windows and Linux only")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)


class FakeSend:
    """Stands in for notify.send. While ``up`` is False no channel takes the alert (the PC's network is down)."""

    def __init__(self) -> None:
        self.up, self.sent, self.tried = True, [], 0

    def __call__(self, alerts, subject, text, account="default"):
        self.tried += 1
        if not self.up:
            return []
        self.sent.append((subject, text))
        return ["webhook"]

    def texts(self) -> list[str]:
        return [text for _subject, text in self.sent]


@pytest.fixture
def alerts(site, monkeypatch):
    edit_site(site, alerts={"on": "failure", "webhook": "env:HOOK"})
    fake = FakeSend()
    monkeypatch.setattr(notify, "send", fake)
    return fake


def edit_site(root, **sections) -> None:
    raw = read_yaml(root / "site.yaml")
    for key, values in sections.items():
        raw[key] = {**(raw.get(key) or {}), **values}
    write_yaml(root / "site.yaml", raw)


def hist(site) -> History:
    return History(site / "data" / "history.sqlite")


def due_daily(site) -> None:
    """Make feed chq run daily at midnight, on a site that has known it for a week (so today's slot is due)."""
    make_feed(site, schedule="daily 00:00")
    with hist(site) as h:
        h.set_meta("feed_seen:chq", (datetime.now(timezone.utc) - timedelta(days=7)).isoformat())


def due(site) -> int:
    return main(["-s", str(site), "run", "--due"])


def task_now(site, now=None) -> dict:
    s = load_site(site)
    feeds, _errors = load_all(s, RecipeLibrary(s.recipe_dirs))
    with History(s.history_path) as h:
        return feedops.task_status(h, feeds, now or datetime.now(timezone.utc), lock_path=s.lock_path)


def warnings_for(site) -> list[dict]:
    s = load_site(site)
    library = RecipeLibrary(s.recipe_dirs)
    feeds, errors = load_all(s, library)
    with History(s.history_path) as h:
        return feedops.site_warnings(s, library, feeds, errors, h, datetime.now(timezone.utc))


# ------------------------------------------------------- LOCK-release-prompt-blocks-schedule
def test_release_does_not_hold_the_lock_while_it_asks(site):
    assert main(["-s", str(site), "run", "chq"]) == 2                 # the first delivery is held
    s = load_site(site)
    feed = load_feeds(s)[0]
    free = []

    def confirm(summary: dict) -> bool:
        with RunLock(s.lock_path):                                    # a scheduled pass could take it now
            free.append(True)
        return True

    res = runner.release(s, feed, None, confirm=confirm)
    assert free == [True] and res.status == "success"
    assert sorted((site / "vendor-drop").glob("*.psv"))


def test_release_sends_nothing_when_the_feed_changed_while_it_asked(site):
    assert main(["-s", str(site), "run", "chq"]) == 2
    s = load_site(site)
    feed = load_feeds(s)[0]

    def confirm(summary: dict) -> bool:
        assert main(["-s", str(site), "run", "chq"]) == 2             # a newer run arrives while the person reads
        return True

    with pytest.raises(FxError, match="While you were deciding"):
        runner.release(s, feed, None, confirm=confirm)
    assert not list((site / "vendor-drop").glob("*"))


def test_release_checks_the_files_again_after_asking(site):
    assert main(["-s", str(site), "run", "chq"]) == 2
    s = load_site(site)
    feed = load_feeds(s)[0]

    def confirm(summary: dict) -> bool:
        path = next((site / "out" / "chq").glob("*/*.psv"))
        path.write_text(path.read_text(encoding="utf-8") + "edited\n", encoding="utf-8")
        return True

    with pytest.raises(FxError, match="changed after it was built"):
        runner.release(s, feed, None, confirm=confirm)
    assert not list((site / "vendor-drop").glob("*"))


def test_a_cancelled_release_still_answers_without_sending(site, monkeypatch, capsys):
    assert main(["-s", str(site), "run", "chq"]) == 2
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert main(["-s", str(site), "release", "chq"]) == 2
    assert "Nothing was sent." in capsys.readouterr().out
    assert not list((site / "vendor-drop").glob("*"))


@can_see_locks
def test_a_lock_held_for_hours_alerts_once(site, alerts):
    due_daily(site)
    s = load_site(site)
    taken = datetime.now(timezone.utc) - timedelta(hours=5)
    with RunLock(s.lock_path):
        os.utime(s.lock_path, (taken.timestamp(), taken.timestamp()))
        assert due(site) == 0
        assert due(site) == 0                                         # the same holder: no second alert
    stuck = [t for t in alerts.texts() if "held this site's lock" in t]
    assert len(stuck) == 1 and "(5 hours)" in stuck[0]


@can_see_locks
def test_a_lock_held_briefly_only_skips_the_hour(site, alerts):
    s = load_site(site)
    with RunLock(s.lock_path):
        assert due(site) == 0
    assert alerts.sent == []


# ------------------------------------------------------- HIST-write-failure-no-alert-heartbeat-green
def disk_full(*args, **kwargs):
    raise sqlite3.OperationalError("database or disk is full")


def test_a_history_error_is_a_plain_message_not_a_traceback(site, monkeypatch, caplog):
    monkeypatch.setattr(History, "start_run", disk_full)
    assert main(["-s", str(site), "run", "chq"]) == 1
    assert "could not be read or written (OperationalError: database or disk is full)" in caplog.text


def test_a_pass_that_cannot_write_history_alerts_and_check_says_the_task_fails(site, alerts, monkeypatch):
    due_daily(site)
    monkeypatch.setattr(History, "set_meta", disk_full)
    monkeypatch.setattr(History, "start_run", disk_full)
    assert due(site) == 1
    assert due(site) == 1
    failed = [t for t in alerts.texts() if t.startswith("The scheduled pass failed:")]
    assert len(failed) == 1                                           # the same failure is not sent every hour
    assert "OperationalError: database or disk is full" in failed[0]
    monkeypatch.undo()
    ended = json.loads((site / "data" / feedops.LAST_PASS).read_text(encoding="utf-8"))
    assert ended == {"error": "OperationalError: database or disk is full", "alerted": True}
    task = task_now(site)
    assert task["state"] == "stale" and task.get("failing")
    assert "The scheduled task runs" in task["text"] and "has not been written at all" in task["text"]
    assert "database or disk is full" in task["text"] and "free space" in task["text"]
    assert "task-failing" in {w["code"] for w in warnings_for(site)}


def test_a_pass_that_fails_otherwise_still_alerts_and_raises(site, alerts, monkeypatch):
    due_daily(site)

    def broken(*args, **kwargs):
        raise RuntimeError("the pass breaks")

    monkeypatch.setattr(runner, "run_many", broken)
    with pytest.raises(RuntimeError):
        due(site)
    assert any(t.startswith("The scheduled pass failed: RuntimeError: the pass breaks") for t in alerts.texts())


def test_history_not_written_for_hours_while_the_task_runs_is_failing(site):
    s = load_site(site)
    now = datetime.now(timezone.utc)
    with hist(site) as h:
        feedops.record_due_start(h, now - timedelta(hours=5))
    feedops.mark_pass_start(s.lock_path, now - timedelta(minutes=10))
    task = task_now(site, now)
    assert task["state"] == "stale" and task.get("failing")
    assert "last started 10 minutes ago" in task["text"] and "has not been written since" in task["text"]


def test_a_healthy_pass_leaves_the_task_ok(site):
    assert due(site) == 0
    task = task_now(site)
    assert task["state"] == "ok" and not task.get("failing")
    ended = json.loads((site / "data" / feedops.LAST_PASS).read_text(encoding="utf-8"))
    assert ended["error"] is None


def test_low_free_space_is_a_warning(site, monkeypatch):
    monkeypatch.setattr(feedops.shutil, "disk_usage", lambda p: types.SimpleNamespace(free=200_000_000))
    low = [w for w in warnings_for(site) if w["code"] == "low-disk"]
    assert low and "Only 0.2 GB is free" in low[0]["text"]
    monkeypatch.setattr(feedops.shutil, "disk_usage", lambda p: types.SimpleNamespace(free=50_000_000_000))
    assert "low-disk" not in {w["code"] for w in warnings_for(site)}


# ------------------------------------------------------- HIST-crash-on-last-attempt-unreported
def test_a_run_left_running_is_failed_and_alerted_even_when_nothing_is_due(site, alerts):
    with hist(site) as h:
        h.start_run("chq", "chq-academic", "1.0.0", "schedule", None)  # a pass killed part way
    assert due(site) == 0                                             # chq (Sun 02:15, new) is not due
    with hist(site) as h:
        assert h.last_run("chq")["status"] == "failed"
    assert any("stopped without finishing" in t for t in alerts.texts())


def test_a_pass_killed_on_the_last_attempt_is_reported_as_given_up(site, alerts):
    edit_site(site, runs={"max_attempts_per_slot": 1})
    due_daily(site)
    with hist(site) as h:
        h.start_run("chq", "chq-academic", "1.0.0", "schedule", None)  # the slot's only attempt, killed
    assert due(site) == 0
    texts = alerts.texts()
    assert any("stopped without finishing" in t for t in texts)
    given_up = [t for t in texts if "was given up after 1 failed attempt(s)" in t]
    assert len(given_up) == 1 and "folio-exchange run chq" in given_up[0]
    assert due(site) == 0
    assert len(alerts.sent) == len(texts)                             # once only


# ------------------------------------------------------- SCHED-outage-exhausts-slot-alerts-lost
def test_an_outage_across_the_slot_is_reported_when_the_network_returns(site, alerts, monkeypatch):
    due_daily(site)
    monkeypatch.setenv("FOLIO_URL", "http://127.0.0.1:1")             # FOLIO out of reach, and so is the webhook
    alerts.up = False
    assert [due(site) for _ in range(4)] == [1, 1, 1, 0]               # three attempts, then the slot is spent
    assert alerts.sent == []
    with hist(site) as h:
        kept = json.loads(h.get_meta(runner.UNSENT))
    assert len(kept) == 4 and "was given up after 3 failed attempt(s)" in kept[-1]["text"]
    alerts.up = True
    assert due(site) == 0                                              # nothing is due, but the alerts go now
    texts = alerts.texts()
    assert len(texts) == 4 and all(t.startswith("First raised ") for t in texts)
    assert sum("Could not update the cache from FOLIO" in t for t in texts) == 3
    assert "was given up after 3 failed attempt(s)" in texts[-1]
    with hist(site) as h:
        assert h.get_meta(runner.UNSENT) is None
    assert due(site) == 0
    assert len(alerts.sent) == 4                                       # sent once, never again


def test_unsent_alerts_wait_while_the_channel_is_still_down(site, alerts):
    alerts.up = False
    s = load_site(site)
    assert runner.alert(s, "chq", "failed", "one") == []
    assert runner.alert(s, "chq", "failed", "two") == []
    with hist(site) as h:
        assert runner.resend_alerts(s, h) == 0
        assert [a["text"] for a in json.loads(h.get_meta(runner.UNSENT))] == ["one", "two"]
        alerts.up = True
        assert runner.resend_alerts(s, h) == 2
    assert [t.split("\n", 1)[1] for t in alerts.texts()] == ["one", "two"]


def test_no_alert_is_kept_when_no_channel_is_set_up(site, monkeypatch):
    monkeypatch.setattr(notify, "send", lambda *a, **k: [])
    edit_site(site, alerts={"on": "always"})
    s = load_site(site)
    runner.alert(s, "chq", "failed", "nowhere to go")
    with hist(site) as h:
        assert h.get_meta(runner.UNSENT) is None


# ------------------------------------------------------- LOGIN-404-abandons-slot
def client_for(folio, password: str = "secret") -> FolioClient:
    return FolioClient(folio.url, "diku", "fx_reader", password, max_retries=1)


def test_a_missing_login_route_is_not_a_refused_password(folio):
    folio.fail_login = [404]                                           # and the legacy route is missing too
    c = client_for(folio)
    with pytest.raises(FolioUnreachable) as caught:
        c.connect()
    assert not isinstance(caught.value, LoginFailed)
    text = str(caught.value)
    assert "/authn/login-with-expiry returned HTTP 404, then /authn/login HTTP 404" in text
    assert "password was not checked" in text
    c.connect()                                                         # FOLIO is back: the same client logs in


def test_an_unexpected_login_status_is_not_a_refused_password(folio):
    folio.fail_login = [418]
    with pytest.raises(FolioUnreachable, match="returned HTTP 418"):
        client_for(folio).connect()


@pytest.mark.parametrize("status", [400, 401, 403, 422])
def test_a_judged_login_is_refused_and_not_retried(folio, status):
    folio.fail_login = [status]
    c = client_for(folio)
    with pytest.raises(LoginFailed, match=f"HTTP {status}"):
        c.connect()
    with pytest.raises(LoginFailed):
        c.connect()                                                     # never asks FOLIO again
    assert sum(r.startswith("POST /authn") for r in folio.requests) == 1


def test_the_legacy_route_refusing_names_both_endpoints(folio):
    folio.fail_login = [404]
    folio.legacy_login = 401
    with pytest.raises(LoginFailed) as caught:
        client_for(folio).connect()
    assert "HTTP 401 from /authn/login; /authn/login-with-expiry returned HTTP 404" in str(caught.value)


def test_a_slot_whose_login_route_was_missing_is_tried_again(site, folio):
    due_daily(site)
    folio.fail_login = [404]
    assert due(site) == 1
    with hist(site) as h:
        first = h.last_run("chq")
    assert first["status"] == "failed" and not first["report"].get("no_retry")
    assert "password was not checked" in first["error"]
    assert due(site) == 2                                               # the next hour tries again: held for review
    with hist(site) as h:
        assert [r["status"] for r in h.runs("chq")] == ["held", "failed"]


# ------------------------------------------------------- review 2b: R-alerts-1, R-alerts-2/3, R-lock-3, R-due-2, R-warn-4
def test_kept_alerts_are_not_sent_again_while_history_cannot_be_written(site, alerts, monkeypatch):
    alerts.up = False
    s = load_site(site)
    for i in range(5):
        runner.alert(s, "chq", "failed", f"kept {i}")
    alerts.up = True
    real = History.set_meta
    monkeypatch.setattr(History, "set_meta", disk_full)              # the disk fills up as the network returns
    assert [due(site) for _ in range(2)] == [1, 1]
    assert not [t for t in alerts.texts() if t.startswith("First raised")]
    monkeypatch.setattr(History, "set_meta", real)                   # space is freed
    assert due(site) == 0
    assert due(site) == 0
    resent = [t.split("\n", 1)[1] for t in alerts.texts() if t.startswith("First raised")]
    assert resent == [f"kept {i}" for i in range(5)]                  # each once, in order


def test_a_kept_alert_is_sent_at_most_once_when_history_fails_after_the_send(site, alerts, monkeypatch):
    alerts.up = False
    s = load_site(site)
    runner.alert(s, "chq", "failed", "one")
    runner.alert(s, "chq", "failed", "two")
    alerts.up = True
    real, calls = History.set_meta, []

    def fails_second(self, key, value):
        calls.append(key)
        if len(calls) > 1:
            disk_full()
        real(self, key, value)

    with hist(site) as h:
        monkeypatch.setattr(History, "set_meta", fails_second)
        assert runner.resend_alerts(s, h) == 1                         # "one" is off the list before it goes
        monkeypatch.setattr(History, "set_meta", real)
        assert runner.resend_alerts(s, h) == 1
    assert [t.split("\n", 1)[1] for t in alerts.texts()] == ["one", "two"]


def test_a_failed_test_alert_is_never_kept_or_sent_later(site, alerts):
    alerts.up = False
    for _ in range(3):
        assert main(["-s", str(site), "check", "--alerts"]) == 1
    with hist(site) as h:
        assert h.get_meta(runner.UNSENT) is None
    alerts.up = True
    assert main(["-s", str(site), "check", "--alerts"]) == 0
    assert due(site) == 0
    assert sum("test alert" in t for t in alerts.texts()) == 1


def test_a_test_alert_kept_by_an_older_version_is_dropped(site, alerts):
    s = load_site(site)
    with hist(site) as h:
        h.set_meta(runner.UNSENT, json.dumps([
            {"subject": f"FOLIO-Exchange [{s.name}]: TEST", "text": "This is a test alert", "raised": None},
            {"subject": f"FOLIO-Exchange [{s.name}] chq: FAILED", "text": "real", "raised": None}]))
        assert runner.resend_alerts(s, h) == 1
        assert h.get_meta(runner.UNSENT) is None
    assert [t.split("\n", 1)[1] for t in alerts.texts()] == ["real"]


def hold_lock_since(site, hours: float) -> RunLock:
    s = load_site(site)
    lock = RunLock(s.lock_path).__enter__()
    taken = datetime.now(timezone.utc) - timedelta(hours=hours)
    os.utime(s.lock_path, (taken.timestamp(), taken.timestamp()))
    return lock


@can_see_locks
def test_a_lock_held_for_hours_sends_no_alert_while_no_feed_is_due(site, alerts, caplog):
    lock = hold_lock_since(site, 5)                                  # a long first sync; chq (Sun 02:15) is not due
    try:
        assert due(site) == 0
    finally:
        lock.__exit__(None, None, None)
    assert alerts.sent == []
    due_daily(site)                                                   # once a feed would have run, it alerts
    lock = hold_lock_since(site, 6)
    try:
        assert due(site) == 0
    finally:
        lock.__exit__(None, None, None)
    assert len([t for t in alerts.texts() if "held this site's lock" in t]) == 1


@can_see_locks
def test_a_lock_held_for_hours_is_stuck_in_check_feeds_and_the_page(site, alerts, capsys):
    due_daily(site)
    lock = hold_lock_since(site, 48)
    try:
        assert due(site) == 0
        task = task_now(site)
        warned = [w for w in warnings_for(site) if w["code"] == "task-stuck"]
        capsys.readouterr()
        assert main(["-s", str(site), "feeds"]) == 0
        out = capsys.readouterr().out
    finally:
        lock.__exit__(None, None, None)
    alert = next(t for t in alerts.texts() if "held this site's lock" in t)
    assert task["state"] == "stuck" and alert == "Scheduled runs are being skipped. " + task["text"] and "(2 days)" in alert
    assert "first sync" not in task["text"]
    assert len(warned) == 1 and warned[0]["level"] == "warning"
    assert "WARNING  One run has held this site's lock since" in out


@can_see_locks
def test_a_lock_held_for_less_than_the_threshold_is_still_running(site):
    lock = hold_lock_since(site, (feedops.LOCK_STUCK_AFTER - timedelta(minutes=10)).total_seconds() / 3600)
    try:
        task = task_now(site)
    finally:
        lock.__exit__(None, None, None)
    assert task["state"] == "running"


def test_a_preview_does_not_alert_an_unchanged_warning_again(site, folio, alerts):
    created = next(t["id"] for t in folio.data["itemNoteTypes"] if t["name"] == "Legacy created date")
    for item in folio.data["items"]:
        item["notes"] = [{"itemNoteTypeId": created, "note": "1998"}]       # no usable date in any of them
    assert main(["-s", str(site), "run", "chq"]) == 2
    assert main(["-s", str(site), "release", "chq", "--yes"]) == 0
    assert main(["-s", str(site), "run", "chq"]) == 0

    def warnings() -> int:
        return sum(subject.endswith("WARNING") for subject, _text in alerts.sent)

    first = warnings()
    assert main(["-s", str(site), "preview", "chq", "-n", "1"]) == 0
    with hist(site) as h:
        shown, real = h.runs("chq", 2)
    assert shown["status"] == "preview" and shown["report"]["warnings"] != real["report"]["warnings"]  # a sample
    assert main(["-s", str(site), "run", "chq"]) == 0
    assert warnings() == first
