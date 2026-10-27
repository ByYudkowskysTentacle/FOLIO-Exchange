"""Second review, settings as a safety boundary: paused clashes, audited added and removed files, what `upgrade`
shows, default_schedule as substance, and key text in a feed file."""
import hashlib
import json
from datetime import datetime, timezone

import pytest
import yaml
from sitekit import make_feed, make_site, write_yaml

from folio_exchange import client as client_mod
from folio_exchange import feedops, runner, setupops, sources
from folio_exchange.cli import main
from folio_exchange.config import BUNDLED_RECIPES, load_site
from folio_exchange.errors import ConfigError, FxError
from folio_exchange.history import History
from folio_exchange.recipe import RecipeLibrary, load_all

RECIPE = BUNDLED_RECIPES / "chq-academic" / "1.0.0.yaml"
KEY_TEXT = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----\n"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)


def hist(site):
    return History(site / "data" / "history.sqlite")


def audit(site) -> list[str]:
    s = load_site(site)
    with History(s.history_path) as h:
        return runner.audit_settings(s, h)


def entries(site, action: str) -> list[dict]:
    with hist(site) as h:
        return [a for a in h.audit_log(200) if a["action"] == action]


def released(site, name="chq"):
    assert main(["-s", str(site), "run", name]) == 2               # a first delivery is held
    assert main(["-s", str(site), "release", name, "--yes"]) == 0


def weakened(version: str) -> dict:
    """The bundled recipe under another version, with its drop guard off, no required fields, suppressed records
    and no MARC read."""
    data = yaml.safe_load(RECIPE.read_text(encoding="utf-8"))
    data["version"] = version
    data["changelog"] = [{"version": version, "date": "2026-10-27", "changes": "Tidier descriptions"}]
    data["checks"] = {"min_item_ratio": 0, "must_be_filled": []}
    data["selection"]["include_suppressed_instances"] = True
    data["reads"] = [r for r in data["reads"] if r != "marc"]
    return data


def warning_codes(site, feed="chq") -> dict[str, str]:
    s = load_site(site)
    library = RecipeLibrary(s.recipe_dirs)
    feeds, errors = load_all(s, library)
    with History(s.history_path) as h:
        found = feedops.site_warnings(s, library, feeds, errors, h, datetime.now(timezone.utc))
    return {w["code"]: w["text"] for w in found if w["feed"] in (feed, None)}


# ------------------------------------------------------------------ COLLIDE-paused-feed-overwrites
def test_a_paused_copy_with_the_same_files_is_not_sent(site):
    released(site)
    drop = site / "vendor-drop"
    before = {p.name: p.read_bytes() for p in drop.iterdir()}
    make_feed(site, name="chq-test", enabled=False, filters={"include_location_codes": ["MAIN"]})
    s = load_site(site)
    assert load_all(s)[1] == {}                                     # the enabled feed still loads and runs
    assert main(["-s", str(site), "run", "chq-test"]) == 1
    with hist(site) as h:
        run = h.last_run("chq-test")
    assert run["status"] == "failed" and "chq-test.yaml" in run["error"] and "feeds/chq.yaml" in run["error"]
    assert {p.name: p.read_bytes() for p in drop.iterdir()} == before
    assert main(["-s", str(site), "run", "chq"]) == 0               # the enabled feed is not stopped


def test_a_paused_copy_that_clashes_after_it_was_held_is_not_released(site, tmp_path):
    make_feed(site, delivery={"transport": "local", "directory": str(tmp_path / "elsewhere")})
    make_feed(site, name="chq-test", enabled=False)                 # same prefix, but chq sends elsewhere for now
    assert main(["-s", str(site), "run", "chq-test"]) == 2          # held: first delivery
    make_feed(site)                                                 # chq now sends to vendor-drop too
    s = load_site(site)
    test = next(f for f in load_all(s)[0] if f.name == "chq-test")
    with pytest.raises(FxError, match="Not released: .*chq.yaml and feeds/chq-test.yaml both send"):
        runner.release(s, test, None)
    assert not (site / "vendor-drop").exists() or not any((site / "vendor-drop").iterdir())


def test_the_page_refuses_a_prefix_that_makes_a_paused_feed_clash(site):
    s = load_site(site)
    library = RecipeLibrary(s.recipe_dirs)
    setupops.add_feed(s, library, "chq-2", "chq-academic")          # paused, file_prefix chq-2
    v = setupops.feed_settings(s, library, "chq-2")["version"]
    setupops.save_delivery(s, library, "chq-2", {"transport": "local", "directory": str(site / "vendor-drop")},
                           version=v)
    v = setupops.feed_settings(s, library, "chq-2")["version"]
    with pytest.raises(ConfigError, match="Not saved: .*both send test_"):
        setupops.save_feed(s, library, "chq-2", {"file_prefix": "test"}, version=v)
    assert setupops.feed_settings(s, library, "chq-2")["file_prefix"] == "chq-2"


# ------------------------------------------------------------------ AUDIT-new-local-recipe-files
def test_a_new_site_first_audit_records_nothing(site):
    assert audit(site) == []
    with hist(site) as h:
        assert h.audit_log(50) == []
    assert audit(site) == []


def test_a_hand_placed_recipe_version_is_audited_and_warned_about(site):
    audit(site)
    path = write_yaml(site / "recipes" / "chq-academic" / "1.0.1.yaml", weakened("1.0.1"))
    assert audit(site) == ["recipes/chq-academic/1.0.1.yaml"]
    (added,) = entries(site, "settings file added")
    assert added["detail"]["file"] == "recipes/chq-academic/1.0.1.yaml"
    assert added["detail"]["settings"]["checks"]["min_item_ratio"] == 0
    text = warning_codes(site)["recipe-local-substance"]
    assert "chq-academic@1.0.1" in text and "checks" in text and "reads" in text and "2.0.0" in text
    path.unlink()
    assert audit(site) == ["recipes/chq-academic/1.0.1.yaml"]
    (removed,) = entries(site, "settings file removed")
    assert removed["detail"]["settings"]["checks"]["must_be_filled"] == []
    assert audit(site) == []                                        # the snapshot went with the file
    assert "recipe-local-substance" not in warning_codes(site)


def test_a_downloaded_or_wording_only_version_is_not_warned_about(site):
    data = yaml.safe_load(RECIPE.read_text(encoding="utf-8"))
    data["version"] = "1.0.1"
    data["description"] = "Clearer words."
    path = write_yaml(site / "recipes" / "chq-academic" / "1.0.1.yaml", data)
    assert "recipe-local-substance" not in warning_codes(site)
    path = write_yaml(path, weakened("1.0.1"))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    (site / "recipes" / "sources.json").write_text(json.dumps(
        {"foliohq": {"files": {"chq-academic/1.0.1.yaml": {"sha256": digest, "commit": "a" * 40}}}}), encoding="utf-8")
    assert "recipe-local-substance" not in warning_codes(site)      # recorded as downloaded
    path.write_text(path.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
    assert "recipe-local-substance" in warning_codes(site)          # changed since it was downloaded


# ------------------------------------------------------------------ AUDIT-new-or-renamed-feed-file
def test_a_renamed_feed_file_is_audited_as_removed_and_added(site):
    audit(site)
    (site / "feeds" / "chq.yaml").unlink()
    make_feed(site, name="main", checks={"min_item_ratio": 0})
    assert sorted(audit(site)) == ["feeds/chq.yaml", "feeds/main.yaml"]
    (removed,) = entries(site, "settings file removed")
    (added,) = entries(site, "settings file added")
    assert removed["feed"] == "chq" and removed["detail"]["file"] == "feeds/chq.yaml"
    assert added["feed"] == "main" and added["detail"]["settings"]["checks"] == {"min_item_ratio": 0}


# ------------------------------------------------------------------ UPGRADE-review-hides-safety-changes
def test_upgrade_shows_and_records_weakened_checks_and_the_hold_names_them(site, capsys):
    released(site)
    write_yaml(site / "recipes" / "chq-academic" / "2.0.0.yaml", weakened("2.0.0"))
    capsys.readouterr()
    assert main(["-s", str(site), "upgrade", "chq", "--to", "chq-academic@^2.0", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "check min_item_ratio changed from 0.9 to 0" in out
    assert "check must_be_filled changed from instance_id, status, material_type to none" in out
    assert "selection include_suppressed_instances changed from off to on" in out
    assert "no longer reads marc from FOLIO" in out
    (upgraded,) = entries(site, "recipe upgraded")
    assert "check min_item_ratio changed from 0.9 to 0" in upgraded["detail"]["changes"]
    assert main(["-s", str(site), "run", "chq"]) == 2
    with hist(site) as h:
        reason = h.last_run("chq")["report"]["held"]
    assert "recipe chq-academic@2.0.0" in reason and "check min_item_ratio changed from 0.9 to 0" in reason


# ------------------------------------------------------------------ SOURCES-default-schedule-not-wording
def test_a_minor_version_that_changes_default_schedule_is_substance(tmp_path):
    from test_sources import SHA, FakeGitHub, newer

    root = make_site(tmp_path / "site", "https://folio.example.org")
    make_feed(root, schedule=None)                                  # follows the recipe's default_schedule
    s = load_site(root)
    manual = newer("1.0.1", "Fewer runs.").replace(b"default_schedule: weekly", b"default_schedule: manual", 1)
    assert manual != newer("1.0.1", "Fewer runs.")
    result = sources.check(s, RecipeLibrary(s.recipe_dirs), "foliohq",
                           http=FakeGitHub({"recipes/chq-academic/1.0.1.yaml": manual}, sha=SHA))
    (item,) = result["recipes"]
    assert item["status"] == "invalid" and "default_schedule" in item["problem"] and result["new"] == []


def test_a_feed_added_from_the_page_keeps_its_own_schedule(site):
    s = load_site(site)
    setupops.add_feed(s, RecipeLibrary(s.recipe_dirs), "chq-2", "chq-academic")
    assert yaml.safe_load((site / "feeds" / "chq-2.yaml").read_text(encoding="utf-8"))["schedule"] == "weekly"


# ------------------------------------------------------------------ SECRET-private-key-text-in-history
def sftp(**extra) -> dict:
    return {"transport": "sftp", "host": "sftp.example.com", "username": "lib",
            "host_key_fingerprint": "SHA256:AbCdEfGhIjKlMnOpQrStUvWxYz0123456789+/abcde", **extra}


@pytest.mark.parametrize("key, value", [("private_key", KEY_TEXT), ("private_key", "keys/a\nb"),
                                        ("known_hosts", "sftp.example.com ssh-ed25519 AAAA\nother ssh-rsa AAAA")])
def test_key_text_in_a_feed_file_is_refused_and_never_stored(site, key, value):
    audit(site)
    make_feed(site, delivery=sftp(**{key: value}))
    s = load_site(site)
    errors = load_all(s)[1]
    assert f"delivery.{key}: holds the key itself" in errors["chq"] and "give the path of the key file" in errors["chq"]
    assert "BEGIN OPENSSH" not in errors["chq"] and "AAAA" not in errors["chq"]
    assert audit(site) == ["feeds/chq.yaml"]
    with hist(site) as h:
        stored = json.dumps(h.audit_log(50)) + json.dumps(h.get_meta("settings:feeds/chq.yaml"))
    assert "BEGIN OPENSSH" not in stored and "AAAA" not in stored and "keys/a" not in stored


def test_redact_masks_key_text_but_keeps_key_paths():
    got = runner.redact({"delivery": {"private_key": KEY_TEXT, "known_hosts": "keys/known_hosts"},
                         "note": KEY_TEXT})
    assert got["delivery"]["private_key"].startswith("[removed")
    assert got["note"].startswith("[removed")
    assert got["delivery"]["known_hosts"] == "keys/known_hosts"
    assert runner.redact({"private_key": "keys/chq.key"}) == {"private_key": "keys/chq.key"}
