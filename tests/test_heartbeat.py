"""Batch 1.2 of the improvement plan: notice when the hourly task stops (SPEC-6), and the webhook part of SEC-4
(docs/review-2026-10-27.md)."""
import logging
import os
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest
from sitekit import write_yaml

from folio_exchange import cli, feedops, notify
from folio_exchange import client as client_mod
from folio_exchange.cli import main
from folio_exchange.config import load_site, read_yaml
from folio_exchange.history import History
from folio_exchange.lock import RunLock
from folio_exchange.recipe import RecipeLibrary, load_all

PING = "https://hc.example.org/ping/0f3c-made-up-9b1d"     # a made-up monitor address: it must never be logged
HOOK = "https://chat.example.org/hooks/made-up-7c2e"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)


@pytest.fixture
def keyring(monkeypatch):
    """An in-memory keyring in place of Windows Credential Manager."""
    store: dict = {}
    fake = types.SimpleNamespace(
        get_password=lambda s, u: store.get((s, u)),
        set_password=lambda s, u, v: store.__setitem__((s, u), v),
        get_keyring=lambda: types.SimpleNamespace(priority=5),
    )
    monkeypatch.setitem(sys.modules, "keyring", fake)
    return store


class FakeHttp:
    """Stands in for requests.get / requests.post and records each call."""

    def __init__(self, status: int = 200, error: Exception | None = None) -> None:
        self.status, self.error, self.calls = status, error, []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return types.SimpleNamespace(status_code=self.status, ok=self.status < 400,
                                     headers={"Location": "https://elsewhere.example.org/x"})


def edit_site(root, **sections) -> None:
    raw = read_yaml(root / "site.yaml")
    for key, values in sections.items():
        raw[key] = {**(raw.get(key) or {}), **values}
    write_yaml(root / "site.yaml", raw)


def warnings_for(root, now=None) -> list[dict]:
    s = load_site(root)
    library = RecipeLibrary(s.recipe_dirs)
    feeds, errors = load_all(s, library)
    with History(s.history_path) as hist:
        return feedops.site_warnings(s, library, feeds, errors, hist, now or datetime.now(timezone.utc))


def set_last_start(root, when: datetime) -> None:
    with History(root / "data" / "history.sqlite") as hist:
        feedops.record_due_start(hist, when)


def codes(ws: list[dict]) -> set[str]:
    return {w["code"] for w in ws}


def logged(site, caplog) -> str:
    log_file = site / "logs" / "folio-exchange.log"
    return caplog.text + (log_file.read_text(encoding="utf-8") if log_file.is_file() else "")


# ------------------------------------------------------------------ last scheduled start
def test_a_due_pass_records_its_start_even_when_nothing_is_due(site):
    before = datetime.now(timezone.utc).replace(microsecond=0)
    assert main(["-s", str(site), "run", "--due"]) == 0
    with History(site / "data" / "history.sqlite") as hist:
        assert hist.runs() == []                                   # nothing was due
        last = feedops.last_due_start(hist)
    assert last is not None and before <= last <= datetime.now(timezone.utc)


def test_a_manual_run_does_not_count_as_the_scheduled_task(site):
    assert main(["-s", str(site), "run", "chq", "--no-deliver"]) == 0
    with History(site / "data" / "history.sqlite") as hist:
        assert feedops.last_due_start(hist) is None


def test_a_brand_new_site_gets_a_note_not_a_warning(site, capsys):
    ws = warnings_for(site)
    note = next(w for w in ws if w["code"] == "task-never")
    assert note["level"] == "note" and "schedule --register" in note["text"]
    assert "task-stale" not in codes(ws)
    assert main(["-s", str(site), "feeds"]) == 0
    out = capsys.readouterr().out
    assert "NOTE     Scheduled runs have not started yet" in out and "WARNING  The scheduled task" not in out


def test_no_task_note_while_no_enabled_feed_runs_on_a_schedule(site):
    assert main(["-s", str(site), "feeds", "pause", "chq"]) == 0
    assert not codes(warnings_for(site)) & {"task-never", "task-stale"}
    set_last_start(site, datetime.now(timezone.utc) - timedelta(days=3))
    assert not codes(warnings_for(site)) & {"task-never", "task-stale"}


def test_the_task_is_stale_after_two_hours(site):
    start = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)
    set_last_start(site, start)
    fine = warnings_for(site, start + timedelta(minutes=110))
    assert not codes(fine) & {"task-never", "task-stale"}
    stale = next(w for w in warnings_for(site, start + timedelta(hours=3)) if w["code"] == "task-stale")
    assert stale["level"] == "warning" and "last ran 3 hours ago" in stale["text"]
    assert "Windows password" in stale["text"] and "schedule --register" in stale["text"]


def test_check_and_feeds_show_the_stale_task(site, capsys):
    set_last_start(site, datetime.now(timezone.utc) - timedelta(hours=3, minutes=5))
    main(["-s", str(site), "check"])
    out = capsys.readouterr().out
    assert "WARNING  The scheduled task last ran 3 hours ago" in out
    assert main(["-s", str(site), "feeds"]) == 0
    assert "WARNING  The scheduled task last ran 3 hours ago" in capsys.readouterr().out


def test_check_and_feeds_show_how_long_ago_a_healthy_task_ran(site, capsys):
    set_last_start(site, datetime.now(timezone.utc) - timedelta(minutes=25))
    main(["-s", str(site), "check"])
    out = capsys.readouterr().out
    assert "The scheduled task last ran 25 minutes ago." in out and "WARNING  The scheduled task" not in out
    main(["-s", str(site), "feeds"])
    assert "The scheduled task last ran 25 minutes ago." in capsys.readouterr().out


def test_an_older_site_counts_its_last_scheduled_run(site):
    with History(site / "data" / "history.sqlite") as hist:
        run_id = hist.start_run("chq", "chq-academic", "1.0.0", "schedule", None)
        hist.finish_run(run_id, "success")
        assert feedops.last_due_start(hist) is not None
    assert "task-never" not in codes(warnings_for(site))


def test_ages_read_plainly():
    assert feedops.ago(timedelta(minutes=1)) == "1 minute"
    assert feedops.ago(timedelta(minutes=119)) == "119 minutes"
    assert feedops.ago(timedelta(hours=5)) == "5 hours"
    assert feedops.ago(timedelta(days=3, hours=2)) == "3 days"
    assert feedops.ago(timedelta(seconds=-30)) == "0 minutes"     # a clock set back


# ------------------------------------------------------------------ a long pass
can_see_locks = pytest.mark.skipif(sys.platform != "win32" and not os.path.exists("/proc/locks"),
                                   reason="held locks are seen on Windows and Linux only")


def lock_path(site):
    return load_site(site).lock_path


@can_see_locks
def test_a_long_pass_is_running_not_stale(site, capsys):
    start = (datetime.now(timezone.utc) - timedelta(hours=3, minutes=30)).replace(second=0, microsecond=0)
    set_last_start(site, start)
    with RunLock(lock_path(site)):
        os.utime(lock_path(site), (start.timestamp() + 30, start.timestamp() + 30))   # took the lock at its start
        task = task_now(site)
        assert task["state"] == "running" and task["since"].startswith(f"{start:%Y-%m-%dT%H:%M}")
        assert "A run has been going since" in task["text"] and "(3 hours)" in task["text"]
        assert not codes(warnings_for(site)) & {"task-never", "task-stale"}
        main(["-s", str(site), "feeds"])
        out = capsys.readouterr().out
        assert "A run has been going since" in out and "WARNING  The scheduled task" not in out
    assert task_now(site)["state"] == "stale"                     # the lock is free again


@can_see_locks
def test_a_manual_run_while_the_task_is_stopped_hides_nothing(site):
    set_last_start(site, datetime.now(timezone.utc) - timedelta(days=2))
    with RunLock(lock_path(site)):                                # taken now, two days after the task last ran
        assert task_now(site)["state"] == "stale"
        assert "task-stale" in codes(warnings_for(site))


def test_a_free_lock_or_no_lock_file_is_not_a_run(site, tmp_path):
    assert feedops.run_started(tmp_path / "missing.lock") is None
    with RunLock(tmp_path / "run.lock"):
        pass
    assert feedops.run_started(tmp_path / "run.lock") is None
    assert task_now(site)["state"] == "never"


def task_now(site) -> dict:
    s = load_site(site)
    feeds, _errors = load_all(s, RecipeLibrary(s.recipe_dirs))
    with History(s.history_path) as hist:
        return feedops.task_status(hist, feeds, datetime.now(timezone.utc), lock_path=s.lock_path)


# ------------------------------------------------------------------ the heartbeat
def test_the_heartbeat_goes_before_and_after_the_pass(site, monkeypatch):
    edit_site(site, alerts={"heartbeat": "env:FX_HEARTBEAT"})
    monkeypatch.setenv("FX_HEARTBEAT", PING)
    http = FakeHttp()
    monkeypatch.setattr(notify.requests, "get", http)
    seen = []

    def run_many(*args, **kwargs):
        seen.append(len(http.calls))                              # the heartbeats sent before any feed runs
        raise RuntimeError("the pass fails")

    monkeypatch.setattr(cli.runner, "run_many", run_many)
    with History(site / "data" / "history.sqlite") as hist:       # make chq due
        hist.set_meta("feed_seen:chq", (datetime.now(timezone.utc) - timedelta(days=8)).isoformat())
    with pytest.raises(RuntimeError):
        main(["-s", str(site), "run", "--due"])
    assert seen == [1] and len(http.calls) == 2                   # one at the start, one at the end anyway

def test_the_heartbeat_is_requested_once_per_pass_and_never_logged(site, monkeypatch, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    edit_site(site, alerts={"heartbeat": "env:FX_HEARTBEAT"})
    monkeypatch.setenv("FX_HEARTBEAT", PING)
    http = FakeHttp()
    monkeypatch.setattr(notify.requests, "get", http)
    assert main(["-s", str(site), "run", "--due"]) == 0                    # nothing due
    assert len(http.calls) == 2                                            # at the start and at the end
    url, kwargs = http.calls[0]
    assert url == PING and kwargs["allow_redirects"] is False and kwargs["timeout"] <= 30
    with History(site / "data" / "history.sqlite") as hist:                # a pass that runs a feed (held)
        hist.set_meta("feed_seen:chq", (datetime.now(timezone.utc) - timedelta(days=8)).isoformat())
    main(["-s", str(site), "run", "--due"])
    with History(site / "data" / "history.sqlite") as hist:
        assert [r["trigger"] for r in hist.runs()] == ["schedule"]
    assert len(http.calls) == 4
    assert main(["-s", str(site), "run", "chq", "--no-deliver"]) == 0      # not a scheduled pass
    assert len(http.calls) == 4
    text = logged(site, caplog) + "".join(capsys.readouterr())
    assert "Heartbeat sent." in text
    assert PING not in text and "hc.example.org" not in text


def test_a_failing_heartbeat_never_fails_the_pass_and_names_no_url(site, monkeypatch, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    edit_site(site, alerts={"heartbeat": "env:FX_HEARTBEAT"})
    monkeypatch.setenv("FX_HEARTBEAT", PING)
    monkeypatch.setattr(notify.requests, "get", FakeHttp(error=notify.requests.ConnectionError(f"no route to {PING}")))
    assert main(["-s", str(site), "run", "--due"]) == 0
    monkeypatch.setattr(notify.requests, "get", FakeHttp(status=302))
    assert main(["-s", str(site), "run", "--due"]) == 0
    text = logged(site, caplog) + "".join(capsys.readouterr())
    assert "Heartbeat failed: ConnectionError" in text and "redirect (HTTP 302)" in text
    assert PING not in text and "elsewhere.example.org" not in text


def test_an_http_heartbeat_is_refused(site, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    http = FakeHttp()
    monkeypatch.setattr(notify.requests, "get", http)
    monkeypatch.setenv("FX_HEARTBEAT", "http://hc.example.org/ping/made-up")
    assert notify.heartbeat({"heartbeat": "env:FX_HEARTBEAT"}, "site") is False
    assert http.calls == [] and "refused" in caplog.text and "hc.example.org" not in caplog.text
    monkeypatch.setenv("FX_HEARTBEAT", "http://127.0.0.1:8099/ping")           # this computer is fine
    assert notify.heartbeat({"heartbeat": "env:FX_HEARTBEAT"}, "site") is True
    assert notify.heartbeat({"heartbeat": None}, "site") is False and len(http.calls) == 1


def test_a_missing_heartbeat_url_is_reported_without_failing(site, monkeypatch, caplog):
    http = FakeHttp()
    monkeypatch.setattr(notify.requests, "get", http)
    edit_site(site, alerts={"heartbeat": "env:FX_HEARTBEAT"})
    assert main(["-s", str(site), "run", "--due"]) == 0
    assert http.calls == [] and "Heartbeat is configured but its URL was not found" in caplog.text


def test_secret_set_heartbeat_stores_it_in_the_keyring(site, monkeypatch, keyring, capsys):
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": PING)
    assert main(["-s", str(site), "secret", "set", "heartbeat"]) == 0
    out = capsys.readouterr().out
    assert keyring == {("folio-exchange/heartbeat", site.name): PING}
    assert "add `heartbeat: keyring` under alerts" in out and PING not in out
    edit_site(site, alerts={"heartbeat": "keyring"})
    main(["-s", str(site), "secret", "status"])
    out = capsys.readouterr().out
    assert f"heartbeat URL: system keyring (folio-exchange/heartbeat, {site.name})" in out and PING not in out
    with History(site / "data" / "history.sqlite") as hist:
        entry = hist.audit_log(5)[0]
    assert entry["action"] == "secret stored" and entry["feed"] is None and PING not in str(entry)
    http = FakeHttp()
    monkeypatch.setattr(notify.requests, "get", http)
    assert main(["-s", str(site), "run", "--due"]) == 0
    assert [c[0] for c in http.calls] == [PING, PING]


# ------------------------------------------------------------------ the webhook (SEC-4)
def test_the_webhook_does_not_follow_a_redirect(monkeypatch, caplog):
    monkeypatch.setenv("FX_HOOK", HOOK)
    http = FakeHttp(status=301)
    monkeypatch.setattr(notify.requests, "post", http)
    assert notify.send({"webhook": "env:FX_HOOK"}, "Subject", "text", account="site") == []
    assert http.calls[0][1]["allow_redirects"] is False
    assert "redirect (HTTP 301)" in caplog.text
    assert "chat.example.org" not in caplog.text and "elsewhere.example.org" not in caplog.text


def test_an_http_webhook_is_refused(monkeypatch, caplog):
    http = FakeHttp()
    monkeypatch.setattr(notify.requests, "post", http)
    monkeypatch.setenv("FX_HOOK", "http://chat.example.org/hooks/made-up")
    assert notify.send({"webhook": "env:FX_HOOK"}, "Subject", "text", account="site") == []
    assert http.calls == [] and "refused" in caplog.text and "chat.example.org" not in caplog.text
    monkeypatch.setenv("FX_HOOK", HOOK)
    assert notify.send({"webhook": "env:FX_HOOK"}, "Subject", "text", account="site") == ["webhook"]


@pytest.mark.parametrize("url,ok", [
    ("https://hooks.example.org/x", True), ("http://localhost:9000/x", True), ("http://[::1]/x", True),
    ("http://hooks.example.org/x", False), ("ftp://hooks.example.org/x", False), ("https:///x", False),
    ("hooks.example.org/x", False),
])
def test_which_alert_urls_are_allowed(url, ok):
    assert (notify.url_problem(url) is None) is ok


# ------------------------------------------------------------------ TLS switched off (SEC-4)
def test_tls_off_is_a_warning_that_suggests_a_ca_bundle(site):
    assert "tls-off" not in codes(warnings_for(site))
    edit_site(site, folio={"verify_tls": False})
    w = next(w for w in warnings_for(site) if w["code"] == "tls-off")
    assert w["level"] == "warning" and "folio.ca_bundle" in w["text"]


@can_see_locks
def test_the_page_says_a_long_pass_is_running(site):
    from folio_exchange import views

    start = (datetime.now(timezone.utc) - timedelta(hours=3)).replace(second=0, microsecond=0)
    set_last_start(site, start)
    with RunLock(lock_path(site)):
        os.utime(lock_path(site), (start.timestamp() + 30, start.timestamp() + 30))
        assert views.site_summary(site)["task"]["state"] == "running"
