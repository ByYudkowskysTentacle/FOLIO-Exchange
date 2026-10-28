"""Regression tests for defects found in the review of the v1 build (26 Oct 2026), one per finding."""
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from sitekit import feed_data, make_feed, write_yaml

from folio_exchange import client as client_mod
from folio_exchange import notify, runner
from folio_exchange import schedule as sched
from folio_exchange.build import BuildReport
from folio_exchange.checks import conformance, required_fields
from folio_exchange.cli import main
from folio_exchange.config import BUNDLED_RECIPES, load_site
from folio_exchange.errors import ConfigError, SafetyCheckFailed
from folio_exchange.history import History
from folio_exchange.recipe import RecipeLibrary, load_feed, validate_recipe


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)


def chq() -> dict:
    return yaml.safe_load((BUNDLED_RECIPES / "chq-academic" / "1.0.0.yaml").read_text(encoding="utf-8"))


def with_alerts(site):
    data = yaml.safe_load((site / "site.yaml").read_text(encoding="utf-8"))
    write_yaml(site / "site.yaml", {**data, "alerts": {"on": "failure", "webhook": "env:HOOK"}})


def test_a_secret_typed_into_a_feed_never_reaches_history(site):
    assert main(["-s", str(site), "check"]) == 0                     # snapshot the settings
    make_feed(site, delivery={"transport": "local", "directory": str(site / "drop"), "secret": "Hunter2-PW",
                              "password": "S3cretPass"})
    main(["-s", str(site), "check"])
    main(["-s", str(site), "history", "--audit"])
    raw = (site / "data" / "history.sqlite").read_bytes()
    assert b"Hunter2-PW" not in raw and b"S3cretPass" not in raw


def test_a_malformed_feed_file_does_not_stop_the_others(site):
    (site / "feeds" / "bad.yaml").write_text(
        "uses: chq-academic@^1.0\nenabled: false\noutputs:\n  items:\n    columns:\n      - {header: X, field: [barcode]}\n",
        encoding="utf-8")
    assert main(["-s", str(site), "run", "chq", "--no-deliver"]) == 0
    assert main(["-s", str(site), "feeds"]) == 1                      # the problem is shown, not a traceback


def test_two_outputs_cannot_write_the_same_file():
    data = chq()
    data["outputs"].append({"name": "locations", "format": "delimited", "extension": "psv",
                            "columns": [{"header": "Item", "field": "item_id"}]})
    data["outputs"][1].pop("file_name")
    assert any("each output needs its own file name" in p for p in validate_recipe(data, "x"))


@pytest.mark.parametrize("text", ["monthly 03:30", "sunrise 03:30", "mo 03:30"])
def test_words_that_start_like_weekdays_are_not_weekdays(text):
    with pytest.raises(ConfigError):
        sched.parse(text)
    assert sched.parse("Monday 03:30").days == (0,) and sched.parse("thurs 03:30").days == (3,)


def test_a_new_feed_is_not_due_for_a_slot_before_the_site_saw_it(site):
    make_feed(site, schedule="daily 00:00")
    assert main(["-s", str(site), "run", "chq"]) == 2                 # held, by hand, after today's slot
    s = load_site(site)
    feeds = [load_feed(site / "feeds" / "chq.yaml", RecipeLibrary(s.recipe_dirs), s.root)]
    with History(s.history_path) as h:
        assert h.get_meta("feed_seen:chq")
        now = datetime.now(timezone.utc)
        if sched.last_slot(sched.parse("daily 00:00"), now, None) > datetime.fromisoformat(
                h.get_meta("feed_seen:chq")):
            pytest.skip("the site first saw the feed before today's slot")
        assert runner.due_feeds(s, feeds, now, h) == []


def test_release_refuses_another_feeds_run(site):
    make_feed(site, name="other", file_prefix="other")
    assert main(["-s", str(site), "run", "other", "--no-deliver"]) == 0
    other_run = sorted((site / "out" / "other").iterdir())[-1]
    assert main(["-s", str(site), "release", "chq", str(other_run), "--yes"]) == 1


def test_a_release_of_what_was_shown_is_refused_when_a_newer_run_failed_meanwhile(site):
    """Regression (safety-3, D2 review): the app sent the run its question named without the comparison or the
    newer-failed-run refusal that the core's own confirm flow applies. With ``expected`` (the summary shown) the
    release looks again as it looked when asking, and refuses when that changed."""
    from folio_exchange import views
    from folio_exchange.errors import FxError

    assert main(["-s", str(site), "run", "chq"]) == 2                          # held
    shown = views.release_summary(site, "chq")
    with pytest.raises(FxError, match="While you were deciding"):              # what was shown is not what is there
        views.release(site, "chq", shown["run_dir"], expected={**shown, "items": 1})
    with History(load_site(site).history_path) as hist:                         # a scheduled run fails meanwhile
        rid = hist.start_run("chq", "chq-academic", "1.0.0", "schedule", None)
        hist.finish_run(rid, "failed", error="FOLIO did not answer.")
    with pytest.raises(FxError, match="failed"):
        views.release(site, "chq", shown["run_dir"], expected=shown)
    assert not (site / "vendor-drop").exists()


def test_a_release_of_what_was_shown_sends_when_nothing_changed(site):
    from folio_exchange import views

    assert main(["-s", str(site), "run", "chq"]) == 2
    shown = views.release_summary(site, "chq")
    assert views.release(site, "chq", shown["run_dir"], expected=shown)["status"] == "success"
    assert (site / "vendor-drop").is_dir()


def test_preview_files_are_marked_even_without_prefix_in_the_name(site):
    make_feed(site, outputs={"items": {"file_name": "library_items_{date}"}, "bibs": {"file_name": "library_{date}"}})
    assert main(["-s", str(site), "preview", "chq", "-n", "2"]) == 0
    prev = sorted((site / "out" / "chq").glob("preview-*"))[-1]
    assert all(p.name.startswith(("PREVIEW_", "run-report")) for p in prev.iterdir())


def test_a_released_blocked_run_no_longer_shows_its_block_message(site, folio):
    assert main(["-s", str(site), "run", "chq"]) == 2
    assert main(["-s", str(site), "release", "chq", "--yes"]) == 0
    folio.data["items"] = folio.data["items"][:1]
    assert main(["-s", str(site), "run", "chq"]) == 2                 # blocked
    assert main(["-s", str(site), "release", "chq", "--yes"]) == 0
    with History(site / "data" / "history.sqlite") as h:
        last = h.last_run("chq")
    assert last["status"] == "success" and last["error"] is None
    rep = json.loads((Path(last["run_dir"]) / "run-report.json").read_text(encoding="utf-8"))
    assert "error" not in rep and "fell from" in rep["released_after"]


def test_conformance_compares_each_output_with_its_own_file(tmp_path, site):
    s = load_site(site)
    feed = load_feed(site / "feeds" / "chq.yaml", RecipeLibrary(s.recipe_dirs), s.root)
    feed.outputs.append({"name": "locations", "format": "delimited", "extension": "psv", "delimiter": "|",
                         "header": True, "columns": [{"header": "Item"}, {"header": "Location"}]})
    (tmp_path / "a_items.psv").write_text("Bib record ID|Barcode\nx|y\n", encoding="utf-8")
    (tmp_path / "b_locations.psv").write_text("Item|Location\n1|2\n", encoding="utf-8")
    samples = {"items": tmp_path / "a_items.psv", "locations": tmp_path / "b_locations.psv"}
    files = {"items": ["a_items.psv"], "locations": ["b_locations.psv"]}
    assert conformance(feed, tmp_path, samples, files) == []


def test_a_paused_feed_with_a_problem_does_not_fail_scheduled_runs(site, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda a, subject, text, account="default": sent.append(subject) or ["x"])
    with_alerts(site)
    make_feed(site, schedule="manual")
    write_yaml(site / "feeds" / "old.yaml", feed_data(site, uses="chq-academic@^9.0", enabled=False))
    assert main(["-s", str(site), "run", "--due"]) == 0
    write_yaml(site / "feeds" / "broken.yaml", feed_data(site, uses="chq-academic@^9.0"))
    for _ in range(3):
        assert main(["-s", str(site), "run", "--due"]) == 1           # still a failure every hour ...
    assert sum("broken" in s for s in sent) == 1                     # ... but one alert per problem


def test_usage_mistakes_exit_1_not_2(capsys):
    with pytest.raises(SystemExit) as exit_:
        main(["run", "--bogus"])
    assert exit_.value.code == 1


def test_history_db_has_no_user_columns(site):
    assert main(["-s", str(site), "run", "chq", "--no-deliver"]) == 0
    db = sqlite3.connect(site / "data" / "cache.sqlite")
    assert {r[1] for r in db.execute("PRAGMA table_info(loans)")} == {
        "id", "item_id", "loan_date", "return_date", "due_date", "status", "renewals", "gen"}
    db.close()


def test_superseded_blocked_runs_are_pruned(site, folio):
    data = yaml.safe_load((site / "site.yaml").read_text(encoding="utf-8"))
    write_yaml(site / "site.yaml", {**data, "runs": {"keep_local_runs": 1}})
    assert main(["-s", str(site), "run", "chq"]) == 2
    assert main(["-s", str(site), "release", "chq", "--yes"]) == 0
    keep = folio.data["items"]
    folio.data["items"] = keep[:1]
    assert main(["-s", str(site), "run", "chq"]) == 2                 # blocked
    blocked = sorted(p for p in (site / "out" / "chq").iterdir() if p.name[:1].isdigit())[-1]
    folio.data["items"] = keep
    assert main(["-s", str(site), "run", "chq"]) == 0                 # supersedes the blocked run
    assert main(["-s", str(site), "run", "chq"]) == 0
    assert not blocked.exists()


def test_manual_builds_do_not_hide_a_stopped_schedule(site):
    make_feed(site, schedule="daily 00:00")
    s = load_site(site)
    with History(s.history_path) as h:
        h.set_meta("feed_seen:chq", "2020-01-01T00:00:00+00:00")
    assert main(["-s", str(site), "run", "chq", "--no-deliver"]) == 0   # built, by hand
    feed = load_feed(site / "feeds" / "chq.yaml", RecipeLibrary(s.recipe_dirs), s.root)
    with History(s.history_path) as h:
        nxt = sched.next_slot(sched.parse("daily 00:00"), datetime.now(timezone.utc), None)
        assert runner.overdue(s, feed, h, nxt + sched.timedelta(hours=1))   # the build was after the slot before


def test_run_folder_names_are_never_reused_and_sort_in_order(site, monkeypatch):
    from folio_exchange import runner as runner_mod

    fixed = datetime(2026, 10, 26, 21, 25, 40, tzinfo=timezone.utc)
    monkeypatch.setattr(runner_mod, "now_utc", lambda: fixed)          # every run in the same second
    data = yaml.safe_load((site / "site.yaml").read_text(encoding="utf-8"))
    write_yaml(site / "site.yaml", {**data, "runs": {"keep_local_runs": 1}})
    for _ in range(3):
        assert main(["-s", str(site), "run", "chq", "--no-deliver"]) == 0
    with History(site / "data" / "history.sqlite") as h:
        dirs = [Path(r["run_dir"]).name for r in reversed(h.runs("chq"))]
    assert dirs == ["20261026-212540", "20261026-212540-02", "20261026-212540-03"]
    assert sorted(dirs) == dirs


def test_an_unfilled_recipe_field_is_not_offered_for_removal_from_the_feed_file(site):
    make_feed(site, checks={"must_be_filled": ["item_hrid"]})
    s = load_site(site)
    feed = load_feed(site / "feeds" / "chq.yaml", RecipeLibrary(s.recipe_dirs), s.root)
    with pytest.raises(SafetyCheckFailed) as only_recipe:
        required_fields(feed, BuildReport(unfilled=Counter({"material_type": 3})))
    msg = str(only_recipe.value)
    assert "material_type: 3 rows" in msg and "requires material_type" in msg
    assert "from checks.must_be_filled" not in msg                   # the feed file cannot drop it
    assert "folio-exchange release chq" in msg
    with pytest.raises(SafetyCheckFailed) as both:
        required_fields(feed, BuildReport(unfilled=Counter({"material_type": 3, "item_hrid": 2})))
    msg = str(both.value)
    assert "remove item_hrid from checks.must_be_filled" in msg
    assert "remove material_type" not in msg and "material_type, item_hrid" not in msg
    with pytest.raises(SafetyCheckFailed) as preview:
        required_fields(feed, BuildReport(unfilled=Counter({"material_type": 3})), preview=True)
    assert "release" not in str(preview.value)                       # a preview cannot be released


def test_a_block_for_unfilled_fields_does_not_talk_about_a_drop(site, folio):
    make_feed(site, checks={"must_be_filled": ["volume"]})
    assert main(["-s", str(site), "run", "chq"]) == 2                 # blocked
    with History(site / "data" / "history.sqlite") as h:
        error = h.last_run("chq")["error"]
    assert "remove volume from checks.must_be_filled" in error and "the drop" not in error.lower()
    # the page leaves out sentences that name a command, so the release must not share one with the fix
    sentences = error.split(". ")
    assert any("checks.must_be_filled in the feed file" in x and "folio-exchange" not in x for x in sentences)
    assert any("folio-exchange release chq" in x for x in sentences)
    assert main(["-s", str(site), "preview", "chq", "-n", "2"]) == 0
    prev = sorted((site / "out" / "chq").glob("preview-*"))[-1]
    warnings = json.loads((prev / "run-report.json").read_text(encoding="utf-8"))["warnings"]
    assert any("A full run would be blocked" in w and "release" not in w for w in warnings)
