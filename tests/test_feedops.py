"""The feed operations shared by the command line and the local page (batch 2.1)."""
import ast
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sitekit import make_feed

from folio_exchange import client as client_mod
from folio_exchange import feedops, ui
from folio_exchange.cli import main
from folio_exchange.config import load_site
from folio_exchange.errors import ConfigError
from folio_exchange.history import History

SRC = Path(__file__).resolve().parents[1] / "src" / "folio_exchange"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)


def test_the_page_imports_nothing_from_the_command_line():
    tree = ast.parse((SRC / "ui.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module not in ("cli", "folio_exchange.cli"), "ui.py imports from cli.py"


def test_a_broken_feed_can_still_be_paused_and_resumed(site):
    make_feed(site, name="broken", uses="chq-academic")         # no version range: does not load
    s = load_site(site)
    res = feedops.set_enabled(s, "broken", False, via="ui")
    assert res == {"name": "broken", "enabled": False, "changed": True}
    text = (site / "feeds" / "broken.yaml").read_text(encoding="utf-8")
    assert "enabled: false" in text and "uses: chq-academic" in text
    assert feedops.set_enabled(s, "broken", False)["changed"] is False
    assert feedops.set_enabled(s, "broken", True)["changed"] is True
    with History(s.history_path) as hist:
        audit = hist.audit_log(50, feed="broken")
    assert {"feed paused", "feed resumed"} <= {a["action"] for a in audit}


def test_pausing_a_missing_feed_is_a_plain_error(site):
    with pytest.raises(ConfigError, match="No feed file"):
        feedops.set_enabled(load_site(site), "nope", False)


def test_the_page_pauses_a_broken_feed(site):
    make_feed(site, name="broken", uses="chq-academic")
    httpd, app = ui.start_in_thread(site)
    try:
        from test_ui import call

        status, body, _ = call(app, "/api/feeds/broken/pause", {})
        assert status == 200 and body == {"name": "broken", "enabled": False}
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_check_lists_paused_feeds_with_a_note(site, capsys):
    assert main(["-s", str(site), "feeds", "pause", "chq"]) == 0
    capsys.readouterr()
    main(["-s", str(site), "check"])
    out = capsys.readouterr().out
    assert "Feed chq:" in out and "[paused]" in out
    assert "NOTE     chq: Paused" in out


def test_check_delivery_lists_paused_feeds(site, capsys):
    assert main(["-s", str(site), "feeds", "pause", "chq"]) == 0
    capsys.readouterr()
    main(["-s", str(site), "check", "--delivery"])
    out = capsys.readouterr().out
    assert "Feed chq:" in out and "[paused]" in out


def test_the_page_shows_the_same_warnings_as_check(site):
    httpd, app = ui.start_in_thread(site)
    try:
        from test_ui import call

        data = call(app, "/api/site")[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
    s, library, feeds, errors = ui._load(site)
    with History(s.history_path) as hist:
        expected = feedops.site_warnings(s, library, feeds, errors, hist, datetime.now(timezone.utc))
    assert [w["code"] for w in data["warnings"]] == [w["code"] for w in expected]
    assert any(w["code"] == "no-alerts" for w in data["warnings"])


def test_a_paused_dry_run_feed_is_not_told_to_test_a_delivery_it_lacks(site, capsys):
    make_feed(site, enabled=False, delivery={"transport": "none", "directory": None})
    main(["-s", str(site), "check"])
    out = capsys.readouterr().out
    assert "NOTE     chq: Paused: scheduled runs skip it. Resume it with `folio-exchange feeds resume chq`." in out
    assert "check --delivery chq" not in out
