"""The site's databases carry a format number (SQLite's user_version), so a version of FOLIO-Exchange never uses
a file a newer one wrote; and a damaged run history is said as such, with what to do."""
import json
import sqlite3

import pytest

from folio_exchange import cache as cache_mod
from folio_exchange import history as history_mod
from folio_exchange.cache import Cache
from folio_exchange.cli import main
from folio_exchange.errors import HistoryDamaged, NewerCache, NewerData
from folio_exchange.history import History


def version_of(path) -> int:
    db = sqlite3.connect(path)
    try:
        return db.execute("PRAGMA user_version").fetchone()[0]
    finally:
        db.close()


def stamp(path, n: int) -> None:
    db = sqlite3.connect(path)
    db.execute(f"PRAGMA user_version = {n}")
    db.commit()
    db.close()


def last_report(site) -> dict:
    with History(site / "data" / "history.sqlite") as h:
        return h.last_run("chq")["report"]


# ------------------------------------------------------------------ the run history
def test_a_new_or_older_history_is_stamped_with_this_versions_format(tmp_path):
    path = tmp_path / "history.sqlite"
    with History(path):
        pass
    assert version_of(path) == history_mod.FORMAT
    stamp(path, 0)                                                  # as a file from 0.2.0 is
    with History(path):
        pass
    assert version_of(path) == history_mod.FORMAT


def digest(path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_a_history_from_a_newer_version_is_refused_and_left_as_it_is(tmp_path):
    path = tmp_path / "history.sqlite"
    db = sqlite3.connect(path)                                      # a newer version's file, not in WAL mode
    db.execute("CREATE TABLE kept (x)")
    db.execute(f"PRAGMA user_version = {history_mod.FORMAT + 1}")
    db.commit()
    db.close()
    before = digest(path)
    with pytest.raises(NewerData, match="written by a newer version of FOLIO-Exchange") as err:
        History(path)
    assert f"its format is {history_mod.FORMAT + 1}" in str(err.value)
    assert digest(path) == before                                   # not a byte written (review of 12 Nov)
    assert not (tmp_path / "history.sqlite-wal").exists()
    path.rename(tmp_path / "moved.sqlite")                          # closed again: Windows lets it be renamed


def test_a_damaged_history_says_so_and_how_to_start_again(tmp_path):
    path = tmp_path / "history.sqlite"
    path.write_bytes(b"this is not a database at all" * 200)
    with pytest.raises(HistoryDamaged, match="rename it") as err:
        History(path)
    assert "held for your review" in str(err.value)
    path.rename(tmp_path / "history.damaged.sqlite")                # closed again: the advice can be followed


def test_the_command_line_says_a_damaged_history_plainly(site, caplog):
    (site / "data").mkdir(exist_ok=True)
    (site / "data" / "history.sqlite").write_bytes(b"\0" * 4096)
    assert main(["-s", str(site), "feeds"]) == 1
    assert "run history" in caplog.text and "is damaged" in caplog.text
    assert "free space" not in caplog.text                          # the wrong advice it used to get


def test_a_newer_history_stops_runs_with_the_reason(site, caplog):
    assert main(["-s", str(site), "run", "chq", "--no-deliver"]) == 0
    stamp(site / "data" / "history.sqlite", history_mod.FORMAT + 1)
    assert main(["-s", str(site), "run", "chq", "--no-deliver"]) == 1
    assert "written by a newer version of FOLIO-Exchange" in caplog.text


# ------------------------------------------------------------------ the cache
def test_a_new_cache_is_stamped_and_a_newer_one_refused(tmp_path):
    path = tmp_path / "cache.sqlite"
    Cache(path).close()
    assert version_of(path) == cache_mod.FORMAT
    stamp(path, cache_mod.FORMAT + 1)
    with pytest.raises(NewerCache, match="newer version"):
        Cache(path)
    path.rename(tmp_path / "moved.sqlite")                          # closed again


def test_a_cache_from_a_newer_version_is_refused_until_it_is_renamed(site, caplog):
    """Refused rather than moved aside (review of 12 Nov): with a scheduled task left on an older program and the
    app on a newer one, each moved the other's cache aside, a full download every hour."""
    assert main(["-s", str(site), "run", "chq", "--no-deliver"]) == 0
    cache = site / "data" / "cache.sqlite"
    stamp(cache, cache_mod.FORMAT + 1)
    assert main(["-s", str(site), "run", "chq", "--no-deliver"]) == 1
    assert "rename the file" in caplog.text and version_of(cache) == cache_mod.FORMAT + 1
    assert not list((site / "data").glob("cache.*-*.sqlite"))       # nothing moved aside
    for suffix in ("", "-wal", "-shm"):
        if cache.with_name(cache.name + suffix).exists():
            cache.with_name(cache.name + suffix).rename(site / "data" / f"cache.old.sqlite{suffix}")
    assert main(["-s", str(site), "run", "chq", "--no-deliver"]) == 0
    assert version_of(cache) == cache_mod.FORMAT
    assert last_report(site)["sync"]["mode"]["instances"] == "full"
    json.dumps(last_report(site))                                   # the report is still whole


# ------------------------------------------------------------------ the hourly pass
@pytest.mark.parametrize("spoil", ["damaged", "newer"])
def test_a_scheduled_pass_with_an_unusable_history_alerts_once_and_keeps_its_heartbeat(site, monkeypatch, spoil):
    """Regression (review of 12 Nov): the history's own errors left the pass before its heartbeat and its alert,
    so a damaged history, or one from a newer version after going back, failed silently every hour."""
    from test_review2_unattended import FakeSend, edit_site

    from folio_exchange import feedops, notify
    from folio_exchange.config import load_site

    edit_site(site, alerts={"on": "failure", "webhook": "env:HOOK"})
    sent, beats = FakeSend(), []
    monkeypatch.setattr(notify, "send", sent)
    monkeypatch.setattr(notify, "heartbeat", lambda alerts, account: beats.append(account) or True)
    path = site / "data" / "history.sqlite"
    if spoil == "damaged":
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"\0" * 4096)
    else:
        with History(path):
            pass
        stamp(path, history_mod.FORMAT + 1)
    assert main(["-s", str(site), "run", "--due"]) == 1
    [text] = sent.texts()
    assert text.startswith("The scheduled pass failed:")
    assert ("is damaged" if spoil == "damaged" else "newer version of FOLIO-Exchange") in text
    assert len(beats) == 2                                          # at the start and the end, as always
    _when, ended = feedops.last_pass(load_site(site).lock_path)
    assert ended.get("alerted") is True and ended.get("error")
    assert main(["-s", str(site), "run", "--due"]) == 1
    assert len(sent.texts()) == 1 and len(beats) == 4              # the same failure isn't sent again
    sent.up = False
    path_note = site / "data" / "last-pass.json"
    path_note.unlink()                                              # forget it was alerted: the alert must be tried
    assert main(["-s", str(site), "run", "--due"]) == 1             # and keeping it for later mustn't crash
