"""What the desktop app's setup views (D3) rely on that needs no Qt: the alert settings, the scheduled task's
state, the app's words for setup, and two D2 fixes (a run's rows that the csv module cannot read, and one
reading of the feeds per refresh). Runs everywhere, with or without the desktop extra."""
from __future__ import annotations

import json
import types

import pytest
from sitekit import make_feed
from test_setup import SFTP_PASSWORD, keyring  # noqa: F401 - the in-memory keyring fixture

from folio_exchange import client as client_mod
from folio_exchange import setupops, siteops, ui, views
from folio_exchange.config import load_site, read_yaml
from folio_exchange.desktop import model
from folio_exchange.errors import ConfigError
from folio_exchange.history import History

SMTP_PASSWORD = "Mail-Pa55-qz17"              # gitleaks:allow - made up; the tests prove it is never written
WEBHOOK = "https://hooks.example.org/services/T000/B000/zz9-very-secret"   # gitleaks:allow - made up


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)


def audit(site):
    with History(load_site(site).history_path) as hist:
        return hist.audit_log(100)


def nowhere_in_files(root, secret: str) -> None:
    for f in root.rglob("*"):
        if f.is_file():
            assert secret.encode() not in f.read_bytes(), f


# ------------------------------------------------------------ D2 fixes
def test_a_value_too_long_for_the_csv_reader_is_a_rows_problem_not_a_crash(site):
    """Regression: csv.Error (a field over the csv module's 131,072-character limit) escaped views.read_rows, so
    the app could not open the run at all and the page answered 500."""
    res = views.run(site, "chq", deliver=False)
    run_id = res["run"]["id"]
    items = next(f for f in res["run"]["files"] if f["name"].endswith((".txt", ".csv", ".psv")))
    path = load_site(site).out_dir / "chq" / res["run"]["folder"] / items["name"]
    with open(path, "a", encoding="utf-8", newline="") as fh:
        fh.write("x" * 200_000 + "\n")
    with pytest.raises(ConfigError, match="could not be read .*field larger than field limit.*Open the run's folder"):
        views.run_rows(site, run_id)
    assert views.run_detail(site, run_id)["id"] == run_id          # the run itself still opens
    app = ui.App(site)
    with pytest.raises(ConfigError):                                # the page answers 400 with the words
        app.get(f"/api/runs/{run_id}/rows", {})


def test_a_snapshot_reads_the_feeds_once_however_many_there_are(site, monkeypatch):
    """Regression: model.snapshot called setupops.feed_settings for every feed, and each loaded every feed again,
    so a refresh took O(n²) loads."""
    for n in range(4):
        make_feed(site, f"extra-{n}", file_prefix=f"extra{n}",
                  delivery={"transport": "local", "directory": str(site / f"drop-{n}")})
    calls = []
    real = setupops.load_all
    monkeypatch.setattr(setupops, "load_all", lambda *a, **k: calls.append(1) or real(*a, **k))
    snap = model.snapshot(site)
    assert calls == [] and len(snap["settings"]) == 5
    assert snap["settings"]["extra-2"]["file_prefix"] == "extra2" and snap["settings"]["chq"]["version"]
    s = load_site(site)
    assert setupops.feed_settings(s, views.load(site)[1], "chq") == snap["settings"]["chq"]    # the same answer


# ------------------------------------------------------------ alerts
def email_form(**kw):
    return {"on": "failure", "email": {"to": "head@lib.example, it@lib.example", "from": "fx@lib.example",
                                       "smtp_host": "SMTP.lib.example", "smtp_port": 587, "starttls": True,
                                       "username": "fx-mailer", **kw}, "webhook": None}


def test_alert_settings_keep_their_secrets_in_the_keyring_only(site, keyring):  # noqa: F811
    before = setupops.alert_settings(load_site(site))
    assert before["on"] == "never" and before["email"] is None and before["webhook"] is None
    out = setupops.save_alerts(site, {**email_form(), "webhook": {"format": "teams"}}, smtp_password=SMTP_PASSWORD,
                               webhook_url=WEBHOOK, version=before["version"])
    raw = read_yaml(site / "site.yaml")["alerts"]
    assert raw["email"] == {"to": ["head@lib.example", "it@lib.example"], "from": "fx@lib.example",
                            "smtp_host": "smtp.lib.example", "smtp_port": 587, "starttls": True,
                            "username": "fx-mailer", "secret": "keyring"}
    assert raw["webhook"] == "keyring" and raw["webhook_format"] == "teams" and raw["on"] == "failure"
    assert keyring[("folio-exchange/smtp", "fx-mailer@smtp.lib.example")] == SMTP_PASSWORD
    assert keyring[("folio-exchange/webhook", "site")] == WEBHOOK            # under the folder's name, as `secret set`
    assert out["changed"] and out["email"]["password"].startswith("system keyring")
    assert out["webhook"]["url"].startswith("system keyring")
    for secret in (SMTP_PASSWORD, WEBHOOK):
        assert secret not in json.dumps(out) and secret not in json.dumps(audit(site))
        nowhere_in_files(site, secret)
    actions = [a["action"] for a in audit(site)]
    assert "alert settings changed from the app" in actions and actions.count("secret stored") == 2
    # empty boxes keep what is stored; nothing changed, nothing written
    again = setupops.save_alerts(site, {**email_form(), "webhook": {"format": "teams"}},
                                 version=out["version"])
    assert again["changed"] is False and keyring[("folio-exchange/smtp", "fx-mailer@smtp.lib.example")] == SMTP_PASSWORD
    # taking the email away leaves only the webhook
    setupops.save_alerts(site, {"on": "always", "email": None, "webhook": {"format": "slack"}}, version=None)
    raw = read_yaml(site / "site.yaml")["alerts"]
    assert "email" not in raw and raw["webhook_format"] == "slack" and raw["on"] == "always"


@pytest.mark.parametrize("form, secrets, problem", [
    (email_form(to="not an address"), {}, "is not an email address"),
    (email_form(**{"from": "nobody"}), {}, "the alerts come from"),
    (email_form(smtp_host="bad host!"), {}, "mail server's name"),
    (email_form(starttls=False), {"smtp_password": "x"}, "needs STARTTLS"),
    (email_form(), {}, "Enter the mail server password for fx-mailer"),
    (email_form(username=""), {"smtp_password": "x"}, "user name too"),
    ({"on": "failure", "webhook": {"format": "slack"}}, {}, "Enter the webhook address"),
    ({"on": "failure", "webhook": {"format": "slack"}}, {"webhook_url": "http://hooks.example.org/x"}, "https://"),
    ({"on": "sometimes"}, {}, "failure, always or never"),
])
def test_alert_settings_that_would_not_work_change_nothing(site, keyring, form, secrets, problem):  # noqa: F811
    before = (site / "site.yaml").read_bytes()
    with pytest.raises(ConfigError, match=problem):
        setupops.save_alerts(site, form, version=None, **secrets)
    assert (site / "site.yaml").read_bytes() == before and keyring == {}


def test_alert_secrets_are_stored_only_once_site_yaml_is_written(site, keyring, monkeypatch):  # noqa: F811
    """Regression: the webhook address and mail password went to the keyring before the last check that site.yaml
    had not changed, so a save refused as "not saved" had replaced the stored secret all the same."""
    path = site / "site.yaml"
    before = setupops.alert_settings(load_site(site))
    real = setupops._site_would_load

    def meanwhile(raw):
        real(raw)
        path.write_bytes(path.read_bytes() + b"\n# edited in another window meanwhile\n")

    monkeypatch.setattr(setupops, "_site_would_load", meanwhile)
    with pytest.raises(ConfigError, match="changed while this was being saved"):
        setupops.save_alerts(site, {"on": "failure", "webhook": {"format": "slack"}}, webhook_url=WEBHOOK,
                             version=before["version"])
    assert keyring == {}
    monkeypatch.setattr(setupops, "_site_would_load", real)

    def locked(*_a):
        raise RuntimeError("the keyring is locked")

    monkeypatch.setattr(setupops.credentials, "store", locked)
    with pytest.raises(ConfigError, match="were saved, but the alert webhook URL could not be stored in the keyring"):
        setupops.save_alerts(site, {"on": "failure", "webhook": {"format": "slack"}}, webhook_url=WEBHOOK,
                             version=None)


def test_the_activity_log_says_where_a_change_was_made(site, monkeypatch):
    """Regression: changes made in the app were logged as "… from the page" (only detail.from said app)."""
    s, library = load_site(site), views.load(site)[1]
    setupops.add_feed(s, library, "extra", "chq-academic@^1.0", {}, via="app")
    setupops.save_feed(s, library, "extra", {"title": "Extra"}, version=None, via="cli")
    setupops.save_delivery(s, library, "extra", {"transport": "none"}, version=None)
    actions = [a["action"] for a in audit(site) if a["feed"] == "extra"]
    assert {"feed added from the app (paused)", "feed settings changed from the command line",
            "feed delivery set from the page"} <= set(actions)


def test_the_test_run_never_reaches_the_real_desktop(tmp_path_factory):
    """A rename in a test carries the desktop launcher along: the Desktop is always a test folder (conftest)."""
    from pathlib import Path

    from folio_exchange import launcher

    assert Path(launcher.desktop()).is_relative_to(tmp_path_factory.getbasetemp())


def test_alert_settings_refuse_a_stale_form_and_a_missing_keyring(site):
    with pytest.raises(ConfigError, match="changed since this form was opened"):
        setupops.save_alerts(site, email_form(username=""), version="stale")
    with pytest.raises(ConfigError, match="No system keyring"):
        setupops.save_alerts(site, email_form(), smtp_password=SMTP_PASSWORD, version=None)
    assert "email" not in (read_yaml(site / "site.yaml").get("alerts") or {})


def test_the_alerts_are_described_without_their_secrets():
    rows = dict(model.alert_rows({"on": "failure", "email": {"to": ["a@x.org"], "from": "f@x.org",
                                                             "smtp_host": "smtp.x.org", "smtp_port": 587,
                                                             "username": "u", "password": "NOT FOUND (nothing)"},
                                  "webhook": {"format": "teams", "url": "environment (HOOK)"}}))
    assert rows["When"].startswith("When a run needs you")
    assert rows["Email"] == "To a@x.org, from f@x.org, through smtp.x.org:587; logs in as u (not stored yet)"
    assert rows["Chat webhook"] == "Microsoft Teams workflow; the address: from the environment variable HOOK"
    assert dict(model.alert_rows({"on": "never"}))["Email"] == "not set up"


# ------------------------------------------------------------ the scheduled task
def test_whether_the_task_is_registered_is_asked_of_schtasks_on_windows_only(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(siteops.platform, "system", lambda: "Linux")
    assert siteops.task_registered(tmp_path / "Lib", run=lambda *a, **k: seen.append(a)) is None and seen == []
    monkeypatch.setattr(siteops.platform, "system", lambda: "Windows")

    def run(argv, **kw):
        seen.append((argv, kw))
        return types.SimpleNamespace(returncode=0)

    assert siteops.task_registered(tmp_path / "Lib", run=run) is True
    argv, kw = seen[0]
    assert argv[1:] == ["/Query", "/TN", "FOLIO-Exchange (Lib)"] and "creationflags" in kw      # no console flashes
    assert siteops.task_registered(tmp_path / "Lib", run=lambda *a, **k: types.SimpleNamespace(returncode=1)) is False


def test_the_tasks_state_names_the_apps_button_not_a_command():
    never = {"state": "never", "text": "Scheduled runs have not started yet: no feed runs on its own until the hourly "
                                       "task is registered. Register it with `folio-exchange schedule --register`."}
    assert model.task_words(never, windows=True) == ("Scheduled runs have not started yet: no feed runs on its own "
                                                     "until the hourly task is registered. Register it under Options, "
                                                     "with Register the scheduled task….")
    # regression: off Windows there is no such button; Options shows the crontab line instead
    assert model.task_words(never, windows=False).endswith("Add the line shown under Options to this computer's "
                                                           "scheduler, with crontab -e.")
    assert model.task_words({"text": "The scheduled task last ran 5 minutes ago."}) == \
        "The scheduled task last ran 5 minutes ago."
    assert model.task_words(None) == "No feed runs on a schedule yet."
    assert "Options" in model.warning_text({"code": "task-never", "text": never["text"]})
    assert "Change alerts" in model.warning_text({"code": "no-alerts"})


# ------------------------------------------------------------ the setup forms' words and checks (the page's)
def test_the_setup_forms_check_what_the_page_checks():
    assert model.free_name("chq", []) == "chq" and model.free_name("chq", ["chq", "chq-2"]) == "chq-3"
    assert model.free_name("folio", []) == "folio-2"                       # reserved
    assert model.feed_name_problem("", []) == "Enter a name for the feed."
    assert "already a feed" in model.feed_name_problem("chq", ["chq"])
    assert "lowercase" in model.feed_name_problem("Bad Name", [])
    assert model.folder_problem("D:\\Exports\\chq") is None and model.folder_problem("\\\\srv\\share\\x") is None
    assert model.folder_problem("/srv/drop") is None and "full path" in model.folder_problem("drop")
    assert model.fingerprint_problem("SHA256:" + "A" * 43) is None and model.fingerprint_problem("") is None
    assert model.fingerprint_problem("MD5:" + ":".join(["0f"] * 16)) is None
    assert "does not look like" in model.fingerprint_problem("abc")
    assert model.schedule_preset("  Sun  03:30 ") == "Sun 03:30" and model.schedule_preset("Mon 01:00") is None
    assert model.schedule_words("manual") == "Only when run by hand"


def test_a_settings_form_sends_only_what_changed_and_says_so():
    base = model.settings_base({"title": "cHQ", "schedule": "Sun 03:30", "file_prefix": "chq",
                                "filters": {"exclude_location_codes": ["A", "B"], "include_suppressed_items": False},
                                "checks": {"min_item_ratio": 0.9}, "legacy": {"circ_count_note_type": None}})
    form = json.loads(json.dumps(base))
    form["filters"]["exclude_location_codes"] = ["B", "A"]                  # the same, in another order
    assert model.settings_changes(base, form) == ({}, [])
    form.update(title="Academic", file_prefix="")
    form["filters"]["include_suppressed_items"] = True
    form["checks"]["min_item_ratio"] = 0.85
    form["legacy"]["circ_count_note_type"] = "Old loans"
    values, changes = model.settings_changes(base, form)
    assert values == {"title": "Academic", "file_prefix": "", "filters": {"include_suppressed_items": True},
                      "checks": {"min_item_ratio": 0.85}, "legacy": {"circ_count_note_type": "Old loans"}}
    assert ("Title", "cHQ", "Academic") in changes and ("File name prefix", "chq", "not set") in changes
    assert ("Block a delivery if the item count falls below", "90% of the last one", "85% of the last one") in changes
    assert ("Loan count from the old system", "none", "Old loans") in changes
    assert model.overlaid(base, {"title": "X", "filters": {"exclude_location_codes": []}})["filters"] == {
        "exclude_location_codes": [], "include_suppressed_items": False}


def test_a_recipe_sources_review_is_put_in_words():
    lines = model.review_lines({"recipes": [
        {"id": "chq-academic", "version": "1.1.0", "status": "new", "title": "collectionHQ Academic",
         "vendor": "collectionHQ", "changes": ["Adds a column."], "reads": ["instances", "items"],
         "patron_data": "none", "picked_up_by": ["chq"]},
        {"id": "chq-academic", "version": "1.0.0", "status": "conflict", "title": "collectionHQ Academic",
         "problem": "A published version must not change."}]})
    assert lines[0] == ("New: collectionHQ Academic 1.1.0 · collectionHQ · not installed yet. What changed: Adds a "
                        "column. Reads from FOLIO: instances, items. Patron data: none. From their next run, these "
                        "feeds use it: chq.")
    assert lines[1].startswith("Refused: collectionHQ Academic 1.0.0 · refused. Refused because: A published")
    assert model.check_words({"commit": "a" * 40, "installed_commit": "a" * 40, "checked": None}).endswith(
        "This is the commit last downloaded.")
    rows = dict(model.source_rows({"repo": "o/r", "vendor": "V", "recipes": ["x"], "commit": None, "origin": "site"}))
    assert rows["Repository"] == "https://github.com/o/r" and rows["Last downloaded"] == "never"
    assert rows["Listed by"] == "this site's own settings"
    assert model.from_words("foliohq", [{"id": "foliohq", "title": "cHQ"}]) == "downloaded from cHQ"


def test_a_permission_check_names_what_is_missing():
    assert model.permission_words({"ok": True})[1] == []
    words, lines = model.permission_words({"ok": False, "denied": ["inventory-storage.items.collection.get"],
                                           "failed": [{"name": "Holdings", "path": "/holdings", "error": "500"}]})
    assert "Ask your FOLIO administrator" in words
    assert lines == ["Permission missing: inventory-storage.items.collection.get",
                     "Could not be read: Holdings (/holdings): 500"]


def test_secrets_typed_in_setup_never_reach_the_audit(site, keyring):  # noqa: F811
    """The SFTP password stored by the app is audited as stored, from the app, and never its value."""
    s = load_site(site)
    lib = views.load(site)[1]
    v = setupops.feed_settings(s, lib, "chq")["version"]
    setupops.save_delivery(s, lib, "chq", {"transport": "sftp", "host": "sftp.vendor.example", "port": 22,
                                           "username": "lib1", "remote_dir": ".", "confirm_later": True},
                           version=v, read_keys=lambda h, p: {"sha256": "SHA256:" + "k" * 43, "md5": "0f:" * 15 + "0f"},
                           via="app")
    setupops.store_sftp_password(s, lib, "chq", SFTP_PASSWORD, via="app")
    entries = audit(site)
    assert {"from": "app"}.items() <= entries[0]["detail"].items() and SFTP_PASSWORD not in json.dumps(entries)
