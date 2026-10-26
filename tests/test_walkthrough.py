"""Regression tests for the operator walkthrough of the v1 build (26 Oct 2026): what a librarian runs into."""
import time

import pytest
import yaml
from sitekit import make_feed, write_yaml

from folio_exchange import client as client_mod
from folio_exchange import schedule as sched
from folio_exchange.cli import main
from folio_exchange.history import History


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)


def test_unreachable_folio_fails_at_once_with_a_plain_message(site, monkeypatch, caplog):
    monkeypatch.setenv("FOLIO_URL", "http://127.0.0.1:1")
    t0 = time.monotonic()
    assert main(["-s", str(site), "run", "chq"]) == 1
    assert time.monotonic() - t0 < 20
    assert "Cannot reach FOLIO at http://127.0.0.1:1 (connection refused)" in caplog.text
    assert caplog.text.count("retrying") == 0


def test_a_refused_login_is_tried_once_and_not_retried_by_the_schedule(site, folio, monkeypatch):
    monkeypatch.setenv("FOLIO_PASSWORD", "wrong")
    make_feed(site, schedule="daily 00:00")
    assert main(["-s", str(site), "run", "chq"]) == 1
    assert sum(r.startswith("POST /authn") for r in folio.requests) == 1
    with History(site / "data" / "history.sqlite") as h:
        run = h.last_run("chq")
    assert run["report"]["no_retry"] is True
    run = {**run, "trigger": "schedule"}
    s = sched.parse("daily 00:00")
    slot = sched.last_slot(s, sched.datetime.now(sched.timezone.utc), None)
    run["started"] = (slot + sched.timedelta(minutes=5)).isoformat()
    assert not sched.is_due(s, slot + sched.timedelta(hours=1), None, [run], 3, None)


def test_release_skips_runs_superseded_by_a_newer_delivery(site, folio):
    assert main(["-s", str(site), "run", "chq"]) == 2
    assert main(["-s", str(site), "release", "chq", "--yes"]) == 0
    keep = folio.data["items"]
    folio.data["items"] = keep[:1]
    assert main(["-s", str(site), "run", "chq"]) == 2                 # blocked
    stale = sorted(p for p in (site / "out" / "chq").iterdir() if p.name[:1].isdigit())[-1]
    folio.data["items"] = keep
    assert main(["-s", str(site), "run", "chq"]) == 0                 # a newer, correct delivery
    assert main(["-s", str(site), "release", "chq", "--yes"]) == 1    # nothing newer to release
    assert main(["-s", str(site), "release", "chq", str(stale), "--yes"]) == 1
    assert main(["-s", str(site), "release", "chq", str(stale), "--yes", "--force"]) == 0


def test_check_blames_the_file_not_the_permissions_for_a_typo(site, capsys):
    text = (site / "feeds" / "chq.yaml").read_text(encoding="utf-8")
    (site / "feeds" / "chq.yaml").write_text(text + "bogus: 1\n", encoding="utf-8")
    assert main(["-s", str(site), "check"]) == 1
    out = capsys.readouterr().out
    assert "Fix the feed file problems shown above" in out and "FOLIO administrator" not in out


def test_feed_messages_use_the_names_in_the_feed_file(site, capsys):
    make_feed(site, filters={"exclude_location_codes": [123]}, checks={"must_be_filled": ["barcod"]})
    assert main(["-s", str(site), "feeds"]) == 1
    out = capsys.readouterr().out
    assert "filters.exclude_location_codes" in out and "selection." not in out
    assert "checks.must_be_filled: unknown field 'barcod' (did you mean 'barcode'?)" in out


def test_a_misspelled_required_column_is_reported_as_a_misspelling(site, capsys):
    data = yaml.safe_load((site / "feeds" / "chq.yaml").read_text(encoding="utf-8"))
    data["outputs"] = {"items": {"columns": [
        {"header": "Bib record ID", "field": "instance_id"}, {"header": "Barcode", "field": "barcod"},
        {"header": "Status", "field": "status"}, {"header": "Material_type", "field": "material_type"}]}}
    write_yaml(site / "feeds" / "chq.yaml", data)
    assert main(["-s", str(site), "feeds"]) == 1
    out = capsys.readouterr().out
    assert "unknown field 'barcod' (did you mean 'barcode'?)" in out and "requires the column 'Barcode'" not in out


def test_upgrade_accepts_a_bare_range_and_says_when_nothing_is_newer(site, capsys):
    assert main(["-s", str(site), "upgrade", "chq"]) == 0
    assert "No newer version of chq-academic" in capsys.readouterr().out
    assert main(["-s", str(site), "upgrade", "chq", "--to", "^9.0", "--yes"]) == 1
    assert main(["-s", str(site), "upgrade", "chq", "--to", "1.0.0", "--yes"]) == 0


def test_pause_twice_says_so(site, capsys):
    assert main(["-s", str(site), "feeds", "pause", "chq"]) == 0
    assert main(["-s", str(site), "feeds", "pause", "chq"]) == 0
    assert "already paused" in capsys.readouterr().out


def test_yaml_layout_errors_are_explained(site, capsys, caplog):
    (site / "feeds" / "chq.yaml").write_text("uses: chq-academic@^1.0\nfilters:\n  exclude_location_codes: [A\n",
                                             encoding="utf-8")
    assert main(["-s", str(site), "feeds"]) == 1
    assert "Check the indentation" in capsys.readouterr().out
