"""The guided setup, driven by scripted answers against the mock FOLIO."""
import yaml
from sitekit import FOLIO_TEST_PASSWORD

from folio_exchange.cli import main
from folio_exchange.wizard import ScriptedPrompter, run_wizard


def answers_until_delivery(url):
    return [url, "diku", "fx_reader",           # FOLIO connection (password from FOLIO_PASSWORD)
            None, None,                         # recipe (the only one), feed name (default chq)
            ["ONLINE"], [],                     # locations and statuses to leave out
            True, "Legacy circ count", "Legacy created date", "",   # legacy note types
            True]                               # preview


def test_wizard_sets_up_a_working_feed(tmp_path, folio, monkeypatch):
    monkeypatch.setenv("FOLIO_PASSWORD", FOLIO_TEST_PASSWORD)
    root = tmp_path / "site"
    drop = tmp_path / "drop"
    p = ScriptedPrompter(answers_until_delivery(folio.url) + ["local", str(drop), None, "none"])
    assert run_wizard(root, p) == 0, "\n".join(p.transcript)
    site = yaml.safe_load((root / "site.yaml").read_text(encoding="utf-8"))
    feed = yaml.safe_load((root / "feeds" / "chq.yaml").read_text(encoding="utf-8"))
    assert site["folio"] == {"base_url": folio.url, "tenant": "diku", "username": "fx_reader", "secret": "keyring"}
    assert feed["uses"] == "chq-academic@^1.0" and feed["enabled"] is True
    assert feed["filters"] == {"exclude_location_codes": ["ONLINE"]}
    assert feed["legacy"] == {"circ_count_note_type": "Legacy circ count",
                              "created_date_note_type": "Legacy created date"}
    assert feed["delivery"] == {"transport": "local", "directory": str(drop)}
    assert feed["schedule"] == "weekly"
    text = "\n".join(p.transcript)
    assert "Preview:" in text and "release chq" in text
    assert "password:" not in (root / "site.yaml").read_text(encoding="utf-8").lower()
    assert main(["-s", str(root), "run", "chq"]) == 2                # first delivery held
    assert main(["-s", str(root), "release", "chq", "--yes"]) == 0
    assert any(drop.iterdir())


def test_wizard_saves_progress_and_stops_on_a_host_key_mismatch(tmp_path, folio, monkeypatch):
    monkeypatch.setenv("FOLIO_PASSWORD", FOLIO_TEST_PASSWORD)
    root = tmp_path / "site"
    p = ScriptedPrompter(answers_until_delivery(folio.url)[:-1] + [False, "sftp", "sftp.vendor.example", "22",
                                                                    "library", ".", "no"])
    code = run_wizard(root, p, read_host_key=lambda host, port: "SHA256:" + "k" * 43)
    assert code == 1
    feed = yaml.safe_load((root / "feeds" / "chq.yaml").read_text(encoding="utf-8"))
    assert feed["enabled"] is False and feed["delivery"]["transport"] == "none"
    assert "host_key_fingerprint" not in feed["delivery"]
    assert "wrong server" in "\n".join(p.transcript)


def test_wizard_stops_before_saving_on_a_bad_login(tmp_path, folio, monkeypatch):
    monkeypatch.setenv("FOLIO_PASSWORD", "wrong")
    p = ScriptedPrompter([folio.url, "diku", "fx_reader"])
    assert run_wizard(tmp_path / "site", p) == 1
    assert not (tmp_path / "site" / "site.yaml").exists()
    assert "Login failed" in "\n".join(p.transcript)
