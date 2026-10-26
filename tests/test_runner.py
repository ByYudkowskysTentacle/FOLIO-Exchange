"""The runner: first-delivery holds, release, scheduled runs, alerts, the audit log and recovery from crashes."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from sitekit import make_feed, write_yaml

from folio_exchange import client as client_mod
from folio_exchange import notify, runner
from folio_exchange.cli import main
from folio_exchange.config import BUNDLED_RECIPES, load_site
from folio_exchange.errors import DeliveryError
from folio_exchange.history import History
from folio_exchange.lock import RunLock
from folio_exchange.recipe import RecipeLibrary, load_feeds


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)


@pytest.fixture
def alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda a, subject, text, account="default": sent.append((subject, text))
                        or ["webhook"])
    return sent


def hist(site):
    return History(site / "data" / "history.sqlite")


def released(site):
    assert main(["-s", str(site), "run", "chq"]) == 2
    assert main(["-s", str(site), "release", "chq", "--yes"]) == 0


def test_hold_is_per_destination(site, tmp_path):
    released(site)
    assert main(["-s", str(site), "run", "chq"]) == 0
    make_feed(site, delivery={"transport": "local", "directory": str(tmp_path / "new-drop")})
    assert main(["-s", str(site), "run", "chq"]) == 2                # a new destination is held again
    with hist(site) as h:
        assert "first delivery to file:" in h.last_run("chq")["report"]["held"]
        assert h.last_delivered("chq")["destination"].endswith("vendor-drop")


def test_major_recipe_upgrade_is_held(site):
    released(site)
    data = json.loads(json.dumps(__import__("yaml").safe_load(
        (BUNDLED_RECIPES / "chq-academic" / "1.0.0.yaml").read_text(encoding="utf-8"))))
    data["version"] = "2.0.0"
    data["changelog"] = [{"version": "2.0.0", "date": "2026-10-27", "changes": "New layout"}]
    write_yaml(site / "recipes" / "chq-academic" / "2.0.0.yaml", data)
    assert main(["-s", str(site), "upgrade", "chq", "--to", "chq-academic@^2.0", "--yes"]) == 0
    assert "uses: chq-academic@^2.0" in (site / "feeds" / "chq.yaml").read_text(encoding="utf-8")
    assert main(["-s", str(site), "run", "chq"]) == 2
    with hist(site) as h:
        assert "recipe chq-academic@2.0.0" in h.last_run("chq")["report"]["held"]
        assert any(a["action"] == "recipe upgraded" for a in h.audit_log())


def test_blocked_run_alerts_with_the_next_command(site, folio, alerts):
    write_yaml(site / "site.yaml", {**__import__("yaml").safe_load((site / "site.yaml").read_text()),
                                    "alerts": {"on": "failure", "webhook": "env:HOOK"}})
    released(site)
    alerts.clear()
    folio.data["items"] = folio.data["items"][:1]
    assert main(["-s", str(site), "run", "chq"]) == 2
    subject, text = alerts[-1]
    assert "chq: BLOCKED" in subject and "fell from" in text and "folio-exchange release chq" in text
    folio.data["items"] = __import__("mock_folio").default_data()["items"]
    assert main(["-s", str(site), "run", "chq"]) == 0
    assert "RECOVERED" in alerts[-1][0]


def test_run_due_runs_good_feeds_and_reports_bad_ones(site, alerts, monkeypatch):
    write_yaml(site / "site.yaml", {**__import__("yaml").safe_load((site / "site.yaml").read_text()),
                                    "alerts": {"on": "failure", "webhook": "env:HOOK"}})
    make_feed(site, schedule="daily 00:00")
    (site / "feeds" / "broken.yaml").write_text("uses: chq-academic\n", encoding="utf-8")
    with hist(site) as h:                                            # the site has known the feed for a week
        h.set_meta("feed_seen:chq", (datetime.now(timezone.utc) - timedelta(days=7)).isoformat())
    code = main(["-s", str(site), "run", "--due"])
    assert code == 1                                                 # worst of: chq held (2), broken failed (1)
    with hist(site) as h:
        assert h.last_run("chq")["status"] == "held" and h.last_run("chq")["trigger"] == "schedule"
    assert any("broken" in subject for subject, _ in alerts)
    assert main(["-s", str(site), "run", "--due"]) == 1              # chq settled (held); broken still reported
    with hist(site) as h:
        assert len(h.runs("chq")) == 1


def test_run_due_skips_quietly_when_another_run_holds_the_lock(site):
    s = load_site(site)
    with RunLock(s.lock_path):
        assert main(["-s", str(site), "run", "--due"]) == 0
        assert main(["-s", str(site), "run", "chq"]) == 1


def test_a_crashed_run_is_marked_failed_and_alerted(site, alerts):
    write_yaml(site / "site.yaml", {**__import__("yaml").safe_load((site / "site.yaml").read_text()),
                                    "alerts": {"on": "failure", "webhook": "env:HOOK"}})
    with hist(site) as h:
        h.start_run("chq", "chq-academic", "1.0.0", "schedule", None)
    assert main(["-s", str(site), "run", "chq", "--no-deliver"]) == 0
    with hist(site) as h:
        assert [r["status"] for r in h.runs("chq")] == ["built", "failed"]
    assert any("never finished" in text or "stopped without finishing" in text for _, text in alerts)


def test_sync_failure_fails_every_feed_once(site, folio, alerts, monkeypatch):
    write_yaml(site / "site.yaml", {**__import__("yaml").safe_load((site / "site.yaml").read_text()),
                                    "alerts": {"on": "failure", "webhook": "env:HOOK"}})
    folio.fail_next = [503] * 500
    assert main(["-s", str(site), "run", "chq"]) == 1
    with hist(site) as h:
        assert h.last_run("chq")["error"].startswith("Sync failed")
    assert len(alerts) == 1 and "no feed ran" in alerts[0][1]


def test_skip_sync_refuses_an_empty_cache(site):
    assert main(["-s", str(site), "build", "chq"]) == 1
    with hist(site) as h:
        assert "never synced" in h.last_run("chq")["error"]


def test_hand_edits_are_audited(site):
    assert main(["-s", str(site), "run", "chq", "--no-deliver"]) == 0
    make_feed(site, checks={"min_item_ratio": 0.5})
    assert main(["-s", str(site), "run", "chq", "--no-deliver", "--skip-sync"]) == 0
    with hist(site) as h:
        entry = next(a for a in h.audit_log() if a["action"] == "settings changed")
    assert entry["detail"]["file"] == "feeds/chq.yaml"
    assert entry["detail"]["changes"]["checks.min_item_ratio"] == {"from": None, "to": 0.5}


def test_partial_delivery_is_recorded(site, monkeypatch):
    released(site)

    def boom(files, d, name):
        raise DeliveryError("stopped part way", ["file:///x/a.mrc"])

    monkeypatch.setattr("folio_exchange.delivery.deliver", boom)
    assert main(["-s", str(site), "run", "chq"]) == 1
    with hist(site) as h:
        last = h.last_run("chq")
        assert last["report"]["partially_delivered"] == ["file:///x/a.mrc"]
        assert not last["delivered"]                                  # never the safety baseline
        assert any(a["action"] == "partial delivery" for a in h.audit_log())


def test_release_of_a_built_run_still_checks_the_item_count(site, folio):
    released(site)
    folio.data["items"] = folio.data["items"][:1]
    assert main(["-s", str(site), "run", "chq", "--no-deliver", "--force"]) == 0
    assert main(["-s", str(site), "release", "chq", "--yes"]) == 2
    assert main(["-s", str(site), "release", "chq", "--yes", "--force"]) == 0


def test_release_asks_first(site, monkeypatch, capsys):
    assert main(["-s", str(site), "run", "chq"]) == 2
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert main(["-s", str(site), "release", "chq"]) == 2
    out = capsys.readouterr().out
    assert "Why it waited: first delivery" in out and "Nothing was sent." in out


def test_old_runs_are_pruned_but_held_runs_kept(site):
    w = __import__("yaml").safe_load((site / "site.yaml").read_text())
    write_yaml(site / "site.yaml", {**w, "runs": {"keep_local_runs": 1}})
    assert main(["-s", str(site), "run", "chq"]) == 2                # held
    held = sorted((site / "out" / "chq").iterdir())[0]
    for _ in range(2):
        assert main(["-s", str(site), "run", "chq", "--no-deliver", "--skip-sync"]) == 0
    assert held.exists()
    assert len([p for p in (site / "out" / "chq").iterdir() if p.name[:1].isdigit()]) == 2


def test_run_feed_api_without_cli(site):
    s = load_site(site)
    feeds = load_feeds(s, RecipeLibrary(s.recipe_dirs))
    results = runner.run_many(s, feeds, runner.RunOptions(no_deliver=True))
    assert [r.status for r in results] == ["built"] and runner.worst_code(results) == 0
