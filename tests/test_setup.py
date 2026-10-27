"""Setting up a site and its feeds from the page (setupops), its API, and the command line hooks the installer uses."""
import io
import json
import os
import subprocess
import sys
import types

import pytest
from sitekit import FOLIO_TEST_PASSWORD, make_feed

from folio_exchange import client as client_mod
from folio_exchange import delivery, schedule, setupops, ui
from folio_exchange.cli import _register_task, build_parser, main
from folio_exchange.config import load_site, read_yaml
from folio_exchange.errors import ConfigError, FxError
from folio_exchange.history import History
from folio_exchange.recipe import RecipeLibrary

SFTP_PASSWORD = "Vendor-Pa55-zq81"  # gitleaks:allow - made up; the tests prove it never reaches a file or response
KEYS = {"sha256": "SHA256:AbCdEfGhIjKlMnOpQrStUvWxYz0123456789+/abcde", "md5": "0f:1e:2d:3c:4b:5a:69:78:87:96:a5:b4:c3:d2:e1:f0"}


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


def lib(site):
    return RecipeLibrary(site.recipe_dirs)


def audit(site, feed=None):
    with History(site.history_path) as hist:
        return hist.audit_log(100, feed)


# ------------------------------------------------------------------ the FOLIO connection
def test_connect_stores_the_password_only_after_folio_accepts_it(tmp_path, folio, keyring):
    root = tmp_path / "newsite"
    with pytest.raises(FxError, match="Could not log in.*\nNothing was saved"):
        setupops.connect(root, folio.url, "diku", "fx_reader", "wrong")
    assert not (root / "site.yaml").exists() and keyring == {}
    state = setupops.connect(root, folio.url + "/", " diku ", "fx_reader", FOLIO_TEST_PASSWORD)
    assert state["ready"] and state["folio"]["password"].startswith("system keyring")
    assert keyring == {("folio-exchange/folio", "fx_reader@diku"): FOLIO_TEST_PASSWORD}
    raw = read_yaml(root / "site.yaml")
    assert raw["folio"] == {"base_url": folio.url, "tenant": "diku", "username": "fx_reader", "secret": "keyring"}
    actions = [a["action"] for a in audit(load_site(root))]
    assert "site settings written from the page" in actions and "secret stored" in actions


def test_connect_keeps_the_stored_password_when_none_is_typed(tmp_path, folio, keyring):
    root = tmp_path / "s"
    setupops.connect(root, folio.url, "diku", "fx_reader", FOLIO_TEST_PASSWORD)
    assert setupops.connect(root, folio.url, "diku", "fx_reader", None)["ready"]
    with pytest.raises(ConfigError, match="Enter the FOLIO password"):
        setupops.connect(tmp_path / "other", folio.url, "diku", "someone-else", None)


def test_connect_never_overwrites_a_site_file_it_cannot_read(tmp_path, folio, keyring):
    (tmp_path / "site.yaml").write_text("folio: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="nothing was changed"):
        setupops.connect(tmp_path, folio.url, "diku", "fx_reader", FOLIO_TEST_PASSWORD)
    assert (tmp_path / "site.yaml").read_text(encoding="utf-8") == "folio: [unclosed\n" and keyring == {}


def test_connect_without_a_keyring_says_what_to_do(tmp_path, folio):
    with pytest.raises(ConfigError, match="FOLIO_PASSWORD"):
        setupops.connect(tmp_path, folio.url, "diku", "fx_reader", FOLIO_TEST_PASSWORD)
    assert not (tmp_path / "site.yaml").exists()


def test_connect_refuses_plain_http_to_another_computer(tmp_path):
    with pytest.raises(ConfigError, match="https://"):
        setupops.connect(tmp_path, "http://folio.example.org", "diku", "fx_reader", "x")


# ------------------------------------------------------------------ feeds
def test_a_new_feed_is_a_paused_dry_run(site):
    s = load_site(site)
    out = setupops.add_feed(s, lib(s), "chq-2", "chq-academic", {"title": "Second branch", "schedule": "daily 04:00"})
    raw = read_yaml(site / "feeds" / "chq-2.yaml")
    assert raw["enabled"] is False and raw["delivery"] == {"transport": "none"}
    assert raw["uses"] == "chq-academic@^1.0" and raw["file_prefix"] == "chq-2"   # never the other feed's files
    assert out["title"] == "Second branch" and out["delivery"]["transport"] == "none"
    assert any(a["action"] == "feed added from the page (paused)" for a in audit(s, "chq-2"))
    with pytest.raises(ConfigError, match="already a feed"):
        setupops.add_feed(s, lib(s), "chq-2", "chq-academic")
    with pytest.raises(ConfigError, match="lowercase"):
        setupops.add_feed(s, lib(s), "Bad Name", "chq-academic")
    with pytest.raises(ConfigError, match="reserved"):
        setupops.add_feed(s, lib(s), "folio", "chq-academic")


def test_adding_a_feed_is_not_blocked_by_another_broken_feed(site):
    make_feed(site, name="broken", uses="chq-academic")          # no version range: does not load
    s = load_site(site)
    setupops.add_feed(s, lib(s), "fresh", "chq-academic")
    assert (site / "feeds" / "fresh.yaml").exists()


def test_editing_settings_keeps_recipe_defaults_out_of_the_file(site):
    s = load_site(site)
    before = setupops.feed_settings(s, lib(s), "chq")
    out = setupops.save_feed(s, lib(s), "chq", {
        "schedule": "Mon 05:00", "checks": {"min_item_ratio": 0.75},
        "filters": {"exclude_location_codes": ["ONLINE", "STAFF"], "include_suppressed_items": False},
    }, version=before["version"])
    assert out["changed"] == ["schedule", "filters", "checks"]
    raw = read_yaml(site / "feeds" / "chq.yaml")
    assert raw["schedule"] == "Mon 05:00" and raw["checks"] == {"min_item_ratio": 0.75}
    assert raw["filters"] == {"exclude_location_codes": ["ONLINE", "STAFF"]}       # False is the recipe's default
    assert out["filters"]["exclude_location_codes"] == ["ONLINE", "STAFF"]
    again = setupops.save_feed(s, lib(s), "chq", {"schedule": "Mon 05:00"}, version=out["version"])
    assert again["changed"] == []
    assert [a["detail"]["changed"] for a in audit(s, "chq") if a["action"] == "feed settings changed from the page"]


def test_a_stale_form_or_a_bad_value_changes_nothing(site):
    s = load_site(site)
    path = site / "feeds" / "chq.yaml"
    version = setupops.feed_settings(s, lib(s), "chq")["version"]
    path.write_text(path.read_text(encoding="utf-8") + "title: edited by hand\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="changed since this form was opened"):
        setupops.save_feed(s, lib(s), "chq", {"schedule": "daily"}, version=version)
    version = setupops.feed_settings(s, lib(s), "chq")["version"]
    before = path.read_bytes()
    for values, problem in (({"schedule": "every blue moon"}, ".+"), ({"delivery": {}}, "cannot be changed here"),
                            ({"checks": {"min_item_ratio": 2}}, "0 to 1"),
                            ({"filters": {"nope": True}}, "not a filter"),
                            ({"file_prefix": "../x"}, "letters, digits")):
        with pytest.raises(ConfigError, match=problem):
            setupops.save_feed(s, lib(s), "chq", values, version=version)
    assert path.read_bytes() == before


def test_a_hand_edited_feed_is_kept_as_bak(site):
    s = load_site(site)
    path = site / "feeds" / "chq.yaml"
    path.write_text("# my notes about this feed\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
    version = setupops.feed_settings(s, lib(s), "chq")["version"]
    setupops.save_feed(s, lib(s), "chq", {"title": "Main"}, version=version)
    assert "# my notes" in (site / "feeds" / "chq.yaml.bak").read_text(encoding="utf-8")


def test_a_change_that_would_clash_with_another_feed_is_put_back(site):
    s = load_site(site)
    make_feed(site, name="other", file_prefix="other")             # same local folder as chq
    s = load_site(site)
    path = site / "feeds" / "other.yaml"
    before = path.read_bytes()
    version = setupops.feed_settings(s, lib(s), "other")["version"]
    with pytest.raises(ConfigError, match="would not load"):
        setupops.save_feed(s, lib(s), "other", {"file_prefix": "test"}, version=version)   # chq's prefix
    assert path.read_bytes() == before and not (site / "feeds" / "other.yaml.bak").exists()


# ------------------------------------------------------------------ delivery
def sftp_form(**kw):
    return {"transport": "sftp", "host": "sftp.vendor.example", "port": 22, "username": "lib1", "remote_dir": ".",
            **kw}


def test_sftp_delivery_pins_the_key_only_when_it_matches_the_vendors(site):
    s = load_site(site)
    path = site / "feeds" / "chq.yaml"
    v = setupops.feed_settings(s, lib(s), "chq")["version"]
    before = path.read_bytes()
    with pytest.raises(ConfigError, match="not the fingerprint you entered"):
        setupops.save_delivery(s, lib(s), "chq", sftp_form(vendor_fingerprint="SHA256:somethingElse"), version=v,
                               read_keys=lambda h, p: KEYS)
    assert path.read_bytes() == before
    with pytest.raises(ConfigError, match="or choose to confirm it later"):
        setupops.save_delivery(s, lib(s), "chq", sftp_form(), version=v, read_keys=lambda h, p: KEYS)
    out = setupops.save_delivery(s, lib(s), "chq", sftp_form(vendor_fingerprint=KEYS["sha256"][7:] + "="),
                                 version=v, read_keys=lambda h, p: KEYS)
    d = read_yaml(path)["delivery"]
    assert d["host_key_fingerprint"] == KEYS["sha256"] and "host_key_confirmed" not in d and d["secret"] == "keyring"
    assert out["delivery"]["host_key_confirmed"] and out["server_key"] == KEYS
    # the same server, already confirmed: no need to enter it again
    setupops.save_delivery(s, lib(s), "chq", sftp_form(remote_dir="in"), version=out["version"],
                           read_keys=lambda h, p: KEYS)
    assert read_yaml(path)["delivery"]["remote_dir"] == "in"


def test_an_md5_fingerprint_from_the_vendor_is_accepted(site):
    s = load_site(site)
    v = setupops.feed_settings(s, lib(s), "chq")["version"]
    setupops.save_delivery(s, lib(s), "chq", sftp_form(vendor_fingerprint="MD5:" + KEYS["md5"].upper()), version=v,
                           read_keys=lambda h, p: KEYS)
    assert read_yaml(site / "feeds" / "chq.yaml")["delivery"]["host_key_fingerprint"] == KEYS["sha256"]


def test_confirming_later_pauses_the_feed_and_keeps_the_key_unconfirmed(site):
    s = load_site(site)
    v = setupops.feed_settings(s, lib(s), "chq")["version"]
    out = setupops.save_delivery(s, lib(s), "chq", sftp_form(confirm_later=True), version=v,
                                 read_keys=lambda h, p: KEYS)
    raw = read_yaml(site / "feeds" / "chq.yaml")
    assert raw["enabled"] is False and raw["delivery"]["host_key_confirmed"] is False
    assert out["delivery"]["host_key_confirmed"] is False
    assert any(a["detail"].get("host_key") == "not confirmed" for a in audit(s, "chq"))


def test_a_feed_with_an_unconfirmed_key_cannot_be_resumed(site, capsys):
    s = load_site(site)
    v = setupops.feed_settings(s, lib(s), "chq")["version"]
    setupops.save_delivery(s, lib(s), "chq", sftp_form(confirm_later=True), version=v, read_keys=lambda h, p: KEYS)
    assert main(["-s", str(site), "feeds", "resume", "chq"]) == 1
    assert "not confirmed with the vendor yet" in capsys.readouterr().err
    assert read_yaml(site / "feeds" / "chq.yaml")["enabled"] is False
    v = setupops.feed_settings(s, lib(s), "chq")["version"]
    setupops.save_delivery(s, lib(s), "chq", sftp_form(vendor_fingerprint=KEYS["sha256"]), version=v,
                           read_keys=lambda h, p: KEYS)
    assert main(["-s", str(site), "feeds", "resume", "chq"]) == 0


def test_delivery_failures_are_put_in_plain_words():
    import socket

    import paramiko

    d = {"host": "sftp.vendor.example", "port": 2222, "username": "lib1"}
    assert "check the server name" in delivery.plain_error(socket.gaierror(11001, "getaddrinfo failed"), d)
    assert "port 2222" in delivery.plain_error(ConnectionRefusedError(), d)
    assert "VPN" in delivery.plain_error(TimeoutError(), d)
    assert "did not accept the login for lib1" in delivery.plain_error(paramiko.AuthenticationException("x"), d)
    assert "ask the vendor" in delivery.plain_error(paramiko.SSHException("Host key for x is A, expected B"), d)
    assert delivery.plain_error(ConfigError("as it is"), d) == "as it is"


def test_an_unreachable_server_changes_nothing(site):
    s = load_site(site)
    v = setupops.feed_settings(s, lib(s), "chq")["version"]

    def down(h, p):
        raise OSError("no route to host")

    with pytest.raises(ConfigError, match="Could not reach sftp.vendor.example:22"):
        setupops.save_delivery(s, lib(s), "chq", sftp_form(vendor_fingerprint=KEYS["sha256"]), version=v,
                               read_keys=down)


def test_a_local_folder_delivery_creates_the_folder(site):
    s = load_site(site)
    v = setupops.feed_settings(s, lib(s), "chq")["version"]
    setupops.save_delivery(s, lib(s), "chq", {"transport": "local", "directory": "drop/here"}, version=v)
    assert (site / "drop" / "here").is_dir()
    assert read_yaml(site / "feeds" / "chq.yaml")["delivery"] == {"transport": "local", "directory": "drop/here"}


# ------------------------------------------------------------------ secrets
def _with_sftp(site):
    s = load_site(site)
    v = setupops.feed_settings(s, lib(s), "chq")["version"]
    setupops.save_delivery(s, lib(s), "chq", sftp_form(vendor_fingerprint=KEYS["sha256"]), version=v,
                           read_keys=lambda h, p: KEYS)
    return s


def test_the_sftp_password_goes_to_the_keyring_only(site, keyring):
    s = _with_sftp(site)
    with pytest.raises(ConfigError, match="Enter the password"):
        setupops.store_sftp_password(s, lib(s), "chq", "")
    out = setupops.store_sftp_password(s, lib(s), "chq", SFTP_PASSWORD)
    assert keyring[("folio-exchange/sftp", "lib1@sftp.vendor.example")] == SFTP_PASSWORD
    assert out["delivery"]["password"].startswith("system keyring")
    assert SFTP_PASSWORD not in json.dumps(out) and SFTP_PASSWORD not in json.dumps(audit(s))
    for f in site.rglob("*"):
        if f.is_file():
            assert SFTP_PASSWORD.encode() not in f.read_bytes(), f


def test_a_password_needs_an_sftp_delivery_first(site, keyring):
    s = load_site(site)
    with pytest.raises(ConfigError, match="Set up the SFTP delivery"):
        setupops.store_sftp_password(s, lib(s), "chq", SFTP_PASSWORD)


def _key_text(passphrase=None):
    import paramiko

    key = paramiko.RSAKey.generate(2048)
    buf = io.StringIO()
    key.write_private_key(buf, password=passphrase)
    return buf.getvalue(), key


def test_a_private_key_is_kept_where_only_this_user_can_read_it(site, keyring):
    s = _with_sftp(site)
    text, key = _key_text("open sesame")
    with pytest.raises(ConfigError, match="protected by a passphrase"):
        setupops.store_private_key(s, lib(s), "chq", text)
    with pytest.raises(ConfigError, match="passphrase is wrong"):
        setupops.store_private_key(s, lib(s), "chq", text, "wrong")
    out = setupops.store_private_key(s, lib(s), "chq", text, "open sesame")
    stored = site / "keys" / "chq.key"
    assert stored.read_text(encoding="utf-8").strip() == text.strip()
    assert out["key"] == f"ssh-rsa {delivery.sha256_fingerprint(key)}" and out["delivery"]["auth"] == "key"
    d = read_yaml(site / "feeds" / "chq.yaml")["delivery"]
    assert d["private_key"] == "keys/chq.key" and keyring[("folio-exchange/sftp", "lib1@sftp.vendor.example")] == \
        "open sesame"
    if os.name == "nt":
        acl = subprocess.run(["icacls", str(stored)], capture_output=True, text=True).stdout
        assert "Everyone" not in acl and "BUILTIN\\Users" not in acl and "(I)" not in acl
    else:
        assert stored.stat().st_mode & 0o077 == 0
    assert "open sesame" not in json.dumps(audit(s)) and "PRIVATE KEY" not in json.dumps(audit(s))
    # back to a password: the key is no longer used, and our copy of it goes
    setupops.store_sftp_password(s, lib(s), "chq", SFTP_PASSWORD)
    assert "private_key" not in read_yaml(site / "feeds" / "chq.yaml")["delivery"] and not stored.exists()


@pytest.mark.parametrize("text,problem", [
    ("PuTTY-User-Key-File-3: ssh-rsa\n", "PuTTYgen"),
    ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI example@pc\n", "not a private key"),
    ("-----BEGIN OPENSSH PRIVATE KEY-----\nnot really\n-----END OPENSSH PRIVATE KEY-----\n", "could not be read"),
])
def test_files_that_are_not_usable_keys_are_explained(site, keyring, text, problem):
    s = _with_sftp(site)
    with pytest.raises(ConfigError, match=problem):
        setupops.store_private_key(s, lib(s), "chq", text)
    assert not (site / "keys" / "chq.key").exists()


# ------------------------------------------------------------------ a changed FOLIO connection
def test_a_cache_from_another_folio_is_set_aside_not_mixed(site, folio, capsys):
    from test_fix_now import last_report

    assert main(["-s", str(site), "run", "chq", "--no-deliver"]) == 0
    raw = read_yaml(site / "site.yaml")
    raw["folio"]["tenant"] = "another"                     # the mock answers any tenant
    from folio_exchange.config import write_yaml

    write_yaml(site / "site.yaml", raw)
    assert main(["-s", str(site), "run", "chq", "--no-deliver", "--skip-sync"]) == 1
    assert "tenant diku, not " in capsys.readouterr().err
    assert main(["-s", str(site), "run", "chq", "--no-deliver"]) == 0
    assert list((site / "data").glob("cache.other-folio-*.sqlite"))
    assert last_report(site)[1]["sync"]["mode"]["instances"] == "full"
    assert main(["-s", str(site), "run", "chq", "--no-deliver", "--skip-sync"]) == 0


# ------------------------------------------------------------------ the page's API
@pytest.fixture
def page(tmp_path):
    from test_ui import call

    servers = []

    def start(root):
        httpd, app = ui.start_in_thread(root)
        servers.append(httpd)
        return lambda path, body=None: call(app, path, body)

    yield start
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


def test_the_page_sets_up_a_new_site_from_nothing(tmp_path, folio, keyring, page, monkeypatch):
    monkeypatch.setattr(delivery, "host_key_fingerprints", lambda h, p, timeout=30: KEYS)
    api = page(tmp_path / "lib")
    (tmp_path / "lib").mkdir()
    status, data, _ = api("/api/site")
    assert status == 200 and data["setup"]["ready"] is False
    status, data, _ = api("/api/setup/connection", {"base_url": folio.url, "tenant": "diku",
                                                    "username": "fx_reader", "password": "wrong"})
    assert status == 400 and "Nothing was saved" in data["error"]
    status, data, _ = api("/api/setup/connection", {"base_url": folio.url, "tenant": "diku",
                                                    "username": "fx_reader", "password": FOLIO_TEST_PASSWORD})
    assert status == 200 and data["ready"]
    status, site_data, _ = api("/api/site")
    assert site_data["setup"] is None and site_data["keyring"] is True and site_data["feeds"] == []
    choices = api("/api/choices")[1]
    assert {"code": "ONLINE", "name": "Online"} in choices["locations"] and "Missing" in choices["statuses"]
    assert api("/api/permissions?uses=chq-academic@^1.0")[1]["ok"] is True
    recipes = api("/api/recipes")[1]
    assert recipes["recipes"][0]["id"] == "chq-academic" and recipes["recipes"][0]["versions"][0]["from"] == "bundled"
    assert recipes["sources"][0]["id"] == "foliohq"
    status, feed, _ = api("/api/feeds", {"name": "chq", "uses": "chq-academic@^1.0",
                                         "values": {"filters": {"exclude_location_codes": ["ONLINE"]}}})
    assert status == 200 and feed["enabled"] is False
    status, feed, _ = api("/api/feeds/chq/settings", {"version": feed["version"], "values": {"title": "cHQ"}})
    assert status == 200 and feed["changed"] == ["title"]
    status, feed, _ = api("/api/feeds/chq/delivery", {"version": feed["version"], "form": sftp_form(
        vendor_fingerprint=KEYS["sha256"])})
    assert status == 200 and feed["server_key"] == KEYS
    status, feed, _ = api("/api/feeds/chq/password", {"password": SFTP_PASSWORD})
    assert status == 200 and SFTP_PASSWORD not in json.dumps(feed)
    assert keyring[("folio-exchange/sftp", "lib1@sftp.vendor.example")] == SFTP_PASSWORD
    assert SFTP_PASSWORD not in json.dumps(api("/api/audit")[1])
    assert api("/api/feeds/chq/settings", {"version": "stale", "values": {"title": "x"}})[0] == 400
    assert api("/api/feeds/chq/settings", {"version": 5, "values": {}})[0] == 400        # not text


def test_the_page_tests_a_local_delivery(site, page):
    from test_ui import wait

    s = load_site(site)
    setupops.save_delivery(s, lib(s), "chq", {"transport": "local", "directory": "drop"},
                           version=setupops.feed_settings(s, lib(s), "chq")["version"])
    httpd, app = ui.start_in_thread(site)
    try:
        from test_ui import call

        job = call(app, "/api/feeds/chq/test-delivery", {})[1]
        done = wait(app, job)
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert done["status"] == "done" and done["result"]["ok"] is True


# ------------------------------------------------------------------ command line hooks for the installer
def test_the_launcher_and_the_page_accept_a_site_still_to_be_set_up(tmp_path, capsys):
    root = tmp_path / "new site"
    out = tmp_path / "launchers"
    out.mkdir()
    assert main(["-s", str(root), "launcher", "--folder", str(out)]) == 0
    assert root.is_dir()
    assert [p.stem for p in out.iterdir()] == ["FOLIO-Exchange (new site)"]
    assert main(["-s", str(root), "run", "--due"]) == 0
    assert "not set up yet" in capsys.readouterr().out
    # without a named folder, nothing is ever started in the current folder
    os.chdir(tmp_path)
    assert main(["ui", "--no-browser"]) == 1


def test_register_runs_schtasks_with_the_exact_task(tmp_path, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr("platform.system", lambda: "Windows")
    args = build_parser().parse_args(["-s", str(tmp_path), "schedule", "--register"])
    assert _register_task(args, run=lambda argv: seen.append(argv) or types.SimpleNamespace(returncode=0)) == 0
    argv = seen[0]
    assert argv[:2] == ["schtasks", "/Create"] and argv[argv.index("/TN") + 1] == schedule.task_name(tmp_path)
    assert argv[argv.index("/RP") + 1] == "*" and argv[-1] == "/F"
    action = argv[argv.index("/TR") + 1]
    assert action.endswith(f'--site "{tmp_path.resolve()}" run --due')
    assert _register_task(args, run=lambda argv: types.SimpleNamespace(returncode=1)) == 1
    assert "did not register" in capsys.readouterr().out


@pytest.mark.skipif(os.name != "nt", reason="Windows command line quoting")
def test_the_registered_action_survives_windows_quoting(tmp_path):
    printer = tmp_path / "printer.py"
    printer.write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8")
    site = tmp_path / "my lib (main)"
    argv = schedule.windows_task_args(site, r"C:\Program Files (x86)\fx\folio-exchange.exe", "PC\\librarian")
    got = json.loads(subprocess.run([sys.executable, str(printer), *argv[1:]], capture_output=True, text=True)
                     .stdout)
    assert got == argv[1:]


# ------------------------------------------------------------------ review findings
def test_connect_refuses_credentials_in_the_address_and_a_site_file_that_would_not_load(tmp_path, folio, keyring):
    with pytest.raises(ConfigError, match="Leave the user name and password out"):
        setupops.connect(tmp_path, "https://admin:pw@folio.example.org", "diku", "fx_reader", "x")
    (tmp_path / "site.yaml").write_text("alerts: {on: sometimes}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="would not load"):
        setupops.connect(tmp_path, folio.url, "diku", "fx_reader", FOLIO_TEST_PASSWORD)
    assert keyring == {} and (tmp_path / "site.yaml").read_text(encoding="utf-8") == "alerts: {on: sometimes}\n"


def test_a_change_made_while_the_server_key_is_read_is_not_overwritten(site):
    s = load_site(site)
    path = site / "feeds" / "chq.yaml"
    v = setupops.feed_settings(s, lib(s), "chq")["version"]

    def slow_read(h, p):                     # another window saves the title meanwhile
        setupops.save_feed(s, lib(s), "chq", {"title": "Saved meanwhile"}, version=v)
        return KEYS

    with pytest.raises(ConfigError, match="changed while this was being saved"):
        setupops.save_delivery(s, lib(s), "chq", sftp_form(vendor_fingerprint=KEYS["sha256"]), version=v,
                               read_keys=slow_read)
    assert read_yaml(path)["title"] == "Saved meanwhile" and read_yaml(path)["delivery"]["transport"] == "local"


def _break_by_hand(site):
    path = site / "feeds" / "chq.yaml"
    raw = read_yaml(path)
    raw["filters"]["no_such_filter"] = True
    from folio_exchange.config import write_yaml

    write_yaml(path, raw)


def test_a_secret_is_not_stored_when_the_feed_would_not_load(site, keyring):
    s = _with_sftp(site)
    text, _key = _key_text("pp")
    setupops.store_private_key(s, lib(s), "chq", text, "pp")
    keyring.clear()
    _break_by_hand(site)
    with pytest.raises(ConfigError, match="would not load"):
        setupops.store_sftp_password(s, lib(s), "chq", SFTP_PASSWORD)
    assert keyring == {} and read_yaml(site / "feeds" / "chq.yaml")["delivery"]["private_key"] == "keys/chq.key"
    with pytest.raises(ConfigError, match="would not load"):
        setupops.store_private_key(s, lib(s), "chq", _key_text("other")[0], "other")
    assert keyring == {} and not list((site / "keys").glob(".chq.*.tmp"))


def test_two_feeds_on_one_account_must_log_in_the_same_way(site, keyring):
    s = _with_sftp(site)
    setupops.store_sftp_password(s, lib(s), "chq", SFTP_PASSWORD)
    setupops.add_feed(s, lib(s), "chq-2", "chq-academic")
    v = setupops.feed_settings(s, lib(s), "chq-2")["version"]
    setupops.save_delivery(s, lib(s), "chq-2", sftp_form(vendor_fingerprint=KEYS["sha256"]), version=v,
                           read_keys=lambda h, p: KEYS)
    with pytest.raises(ConfigError, match="Feed chq logs in to the same account .* with a password"):
        setupops.store_private_key(s, lib(s), "chq-2", _key_text("pp")[0], "pp")
    assert keyring[("folio-exchange/sftp", "lib1@sftp.vendor.example")] == SFTP_PASSWORD


def test_switching_away_from_sftp_removes_the_saved_key(site, keyring):
    s = _with_sftp(site)
    setupops.store_private_key(s, lib(s), "chq", _key_text()[0])
    assert (site / "keys" / "chq.key").exists()
    v = setupops.feed_settings(s, lib(s), "chq")["version"]
    setupops.save_delivery(s, lib(s), "chq", {"transport": "none"}, version=v)
    assert not (site / "keys" / "chq.key").exists()


def _ed25519_openssh(password: bytes | None = None) -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    enc = serialization.BestAvailableEncryption(password) if password else serialization.NoEncryption()
    return Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                                      enc).decode()


def test_a_damaged_key_or_a_very_slow_passphrase_is_a_plain_refusal(site, keyring):
    import base64

    s = _with_sftp(site)
    good = _ed25519_openssh()
    body = "".join(good.splitlines()[1:-1])
    blob = bytearray(base64.b64decode(body))
    blob[-40:-20] = bytes(20)                                  # damage the private part
    damaged = good.splitlines()[0] + "\n" + base64.b64encode(bytes(blob)).decode() + "\n" + good.splitlines()[-1]
    with pytest.raises(ConfigError, match="could not be read"):
        setupops.store_private_key(s, lib(s), "chq", damaged)
    slow = _ed25519_openssh(b"pp")
    blob = bytearray(base64.b64decode("".join(slow.splitlines()[1:-1])))
    assert setupops._kdf_rounds(slow) == 16
    i = blob.index(b"bcrypt") + len(b"bcrypt") + 4               # the options string: salt, then rounds
    salt = int.from_bytes(blob[i:i + 4], "big")
    blob[i + 4 + salt:i + 8 + salt] = (2 ** 31).to_bytes(4, "big")
    slow = slow.splitlines()[0] + "\n" + base64.b64encode(bytes(blob)).decode() + "\n" + slow.splitlines()[-1]
    with pytest.raises(ConfigError, match="2,147,483,648 rounds"):
        setupops.store_private_key(s, lib(s), "chq", slow, "pp")
    assert setupops.store_private_key(s, lib(s), "chq", _ed25519_openssh(b"pp"), "pp")["key"].startswith("ssh-ed25519")


def test_resuming_a_feed_never_breaks_a_working_neighbour(site, capsys):
    make_feed(site, name="copy", file_prefix="test", enabled=False)       # chq's prefix and folder, paused
    assert main(["-s", str(site), "feeds", "resume", "copy"]) == 1
    assert "would stop feed chq from loading" in capsys.readouterr().err
    assert read_yaml(site / "feeds" / "copy.yaml")["enabled"] is False
    from folio_exchange.recipe import load_all

    s = load_site(site)
    assert load_all(s, lib(s))[1] == {}                                   # chq still loads


def test_an_existing_site_without_its_site_file_stays_an_error(site, tmp_path, capsys):
    (site / "site.yaml").rename(site / "site.yaml.moved")
    assert main(["-s", str(site), "run", "--due"]) == 1
    capsys.readouterr()
    assert main(["-s", str(tmp_path / "no" / "such" / "place"), "ui", "--no-browser"]) == 1
    assert "does not exist" in capsys.readouterr().err
    assert not (tmp_path / "no").exists()
