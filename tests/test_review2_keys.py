"""Second review round: another FOLIO is held, a full disk is not damage, the task runs on battery, a pinned SFTP key
keeps working when the server adds a key type, held runs survive a moved site folder, and a lost alert URL shows."""
import io
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import types
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sitekit import make_feed

from folio_exchange import client as client_mod
from folio_exchange import delivery, feedops, runner, schedule, setupops, ui
from folio_exchange.cli import main
from folio_exchange.config import load_site, read_yaml, write_yaml
from folio_exchange.errors import FxError
from folio_exchange.history import History
from folio_exchange.recipe import RecipeLibrary, load_all, validate_recipe


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


def hist(site):
    return History(site / "data" / "history.sqlite")


def released(site):
    assert main(["-s", str(site), "run", "chq"]) == 2              # first delivery to the folder is held
    assert main(["-s", str(site), "release", "chq", "--yes"]) == 0


def edit_site(root, **sections):
    raw = read_yaml(root / "site.yaml")
    for key, values in sections.items():
        raw[key] = {**(raw.get(key) or {}), **values}
    write_yaml(root / "site.yaml", raw)


# ------------------------------------------------------------ HOLD-other-folio-unheld
def test_the_first_delivery_from_another_folio_is_held(site, monkeypatch):
    released(site)
    with hist(site) as h:
        row = h.last_delivered("chq")
    source = row["folio_source"]
    assert source.endswith(" tenant diku") and row["report"]["folio_source"] == source
    monkeypatch.setenv("FOLIO_TENANT", "test")                      # the mock answers any tenant
    assert main(["-s", str(site), "run", "chq"]) == 2
    with hist(site) as h:
        run = h.last_run("chq")
    assert run["status"] == "held" and not run["delivered"]
    assert run["report"]["held"] == f"first delivery from FOLIO {source[:-4]}test; the last came from {source}"
    json_report = json.loads((Path(run["run_dir"]) / "run-report.json").read_text(encoding="utf-8"))
    assert json_report["folio_source"] == run["folio_source"] == source[:-4] + "test"
    assert main(["-s", str(site), "release", "chq", "--yes"]) == 0
    assert main(["-s", str(site), "run", "chq"]) == 0               # the same FOLIO again: delivered


def test_a_delivery_that_recorded_no_folio_is_taken_as_the_same(site, monkeypatch):
    released(site)
    with hist(site) as h:                                           # a row written before sources were kept
        row = h.last_delivered("chq")
        old = {k: v for k, v in row["report"].items() if k != "folio_source"}
        h.db.execute("UPDATE runs SET report=?, folio_source=NULL WHERE id=?", (json.dumps(old), row["id"]))
        h.db.commit()
    monkeypatch.setenv("FOLIO_TENANT", "test")
    assert main(["-s", str(site), "run", "chq"]) == 0


def test_an_older_history_file_gains_the_folio_source_column(tmp_path):
    path = tmp_path / "history.sqlite"
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, feed TEXT NOT NULL, recipe TEXT,
        recipe_version TEXT, trigger TEXT, run_dir TEXT, started TEXT NOT NULL, finished TEXT,
        status TEXT NOT NULL DEFAULT 'running', items INTEGER, bibs INTEGER, delivered INTEGER NOT NULL DEFAULT 0,
        files TEXT, destinations TEXT, error TEXT, report TEXT, actor TEXT, destination TEXT)""")
    db.execute("INSERT INTO runs(feed, started, status, delivered) VALUES ('chq', '2026-01-01T00:00:00', 'success', 1)")
    db.commit()
    db.close()
    with History(path) as h:
        assert h.last_delivered("chq")["folio_source"] is None
        h.start_run("chq", "r", "1.0.0", "manual", None, folio_source="x tenant y")
        assert h.last_run("chq")["folio_source"] == "x tenant y"
        assert h.delivered_feeds() == ["chq"]


def test_the_connect_form_says_feeds_will_hold_after_a_change_of_folio(site, folio, keyring):
    released(site)
    same = setupops.connect(site, folio.url, "diku", "fx_reader")
    assert "notice" not in same
    moved = setupops.connect(site, folio.url + "/", "test", "fx_reader")
    assert "holds its next run for review" in moved["notice"] and "tenant test instead of" in moved["notice"]
    assert main(["-s", str(site), "run", "chq"]) == 2


def test_source_change_note():
    a = {"base_url": "https://folio.example.org/", "tenant": "lib"}
    assert runner.source_change_note(None, a) is None
    assert runner.source_change_note(a, {"base_url": "https://FOLIO.example.org", "tenant": "lib"}) is None
    note = runner.source_change_note(a, {"base_url": "https://test.example.org", "tenant": "lib"})
    assert "https://test.example.org tenant lib instead of https://folio.example.org tenant lib" in note


# ------------------------------------------------------- R-HOLD-1, R-release-1: what a built or blocked run would hold
def release_summary(site) -> dict:
    s = load_site(site)
    feed = next(f for f in load_all(s, RecipeLibrary(s.recipe_dirs))[0] if f.name == "chq")
    seen: dict = {}
    runner.release(s, feed, None, confirm=lambda summary: seen.update(summary) or False)
    return seen


def test_a_smaller_run_from_another_folio_is_blocked_and_names_the_change(site, folio, monkeypatch, capsys):
    sent: list = []
    monkeypatch.setattr("folio_exchange.notify.send", lambda alerts, subject, text, account="default":
                        sent.append(text) or ["webhook"])
    edit_site(site, alerts={"on": "failure", "webhook": "env:HOOK"})
    released(site)
    with hist(site) as h:
        source = h.last_delivered("chq")["folio_source"]
    monkeypatch.setenv("FOLIO_TENANT", "test")                      # a smaller test tenant
    folio.data["items"] = folio.data["items"][:3]
    assert main(["-s", str(site), "run", "chq"]) == 2
    with hist(site) as h:
        run = h.last_run("chq")
    change = f"first delivery from FOLIO {source[:-4]}test; the last came from {source}"
    assert run["status"] == "blocked" and run["report"]["would_hold"] == change
    assert change in run["error"] and "held" not in run["report"]
    assert change in sent[-1]                                        # the alert names it too
    reason = release_summary(site)["reason"]
    assert change in reason and reason.count(change) == 1
    capsys.readouterr()
    assert main(["-s", str(site), "release", "chq", "--yes"]) == 0
    assert change in capsys.readouterr().out


def test_a_built_run_from_another_folio_names_the_change_when_released(site, monkeypatch, capsys):
    released(site)
    monkeypatch.setenv("FOLIO_TENANT", "test")
    assert main(["-s", str(site), "run", "chq", "--no-deliver"]) == 0
    with hist(site) as h:
        run = h.last_run("chq")
    assert run["status"] == "built" and "from FOLIO" in run["report"]["would_hold"]
    assert release_summary(site)["reason"].startswith("a delivery would be held for review: first delivery from FOLIO")
    capsys.readouterr()
    assert main(["-s", str(site), "release", "chq", "--yes"]) == 0
    assert "Why it waited: a delivery would be held for review: first delivery from FOLIO" in capsys.readouterr().out


def test_a_built_run_after_a_major_upgrade_shows_the_upgrade_review(site, capsys):
    from folio_exchange.config import BUNDLED_RECIPES
    released(site)
    data = read_yaml(BUNDLED_RECIPES / "chq-academic" / "1.0.0.yaml")
    data["version"] = "2.0.0"
    data["changelog"] = [{"version": "2.0.0", "date": "2026-10-27", "changes": "No drop guard"}]
    data["checks"] = {**data.get("checks", {}), "min_item_ratio": 0}
    write_yaml(site / "recipes" / "chq-academic" / "2.0.0.yaml", data)
    assert main(["-s", str(site), "upgrade", "chq", "--to", "chq-academic@^2.0", "--yes"]) == 0
    assert main(["-s", str(site), "run", "chq", "--no-deliver"]) == 0
    reason = release_summary(site)["reason"]
    assert "recipe chq-academic@2.0.0" in reason and "check min_item_ratio changed from 0.9 to 0" in reason


def test_a_held_runs_reason_is_unchanged(site):
    assert main(["-s", str(site), "run", "chq"]) == 2
    with hist(site) as h:
        dest = h.last_run("chq")["destination"]
    assert release_summary(site)["reason"] == f"first delivery to {dest}"


# ------------------------------------------------------- CACHE-diskfull-treated-as-corruption
def broken_copies(site):
    return sorted(p.name for p in (site / "data").glob("cache.broken-*"))


def test_a_full_disk_during_sync_keeps_the_cache(site, monkeypatch):
    assert main(["-s", str(site), "run", "chq", "--no-deliver"]) == 0
    cache = site / "data" / "cache.sqlite"
    size = cache.stat().st_size

    def full(*_a, **_k):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(runner.Syncer, "run", full)
    assert main(["-s", str(site), "run", "chq", "--no-deliver"]) == 1
    with hist(site) as h:
        error = h.last_run("chq")["error"]
    assert "The disk is full" in error and "left as it is" in error and "damaged" not in error
    assert cache.is_file() and cache.stat().st_size >= size and broken_copies(site) == []


def test_a_cache_that_cannot_be_opened_is_not_moved_aside(site, monkeypatch):
    assert main(["-s", str(site), "run", "chq", "--no-deliver"]) == 0

    def cannot_open(_path):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(runner, "Cache", cannot_open)
    s = load_site(site)
    with pytest.raises(FxError, match="could not be opened"):
        runner.sync(s, {"instances"})
    assert (site / "data" / "cache.sqlite").is_file() and broken_copies(site) == []


def test_a_full_disk_while_building_keeps_the_cache(site, monkeypatch):
    assert main(["-s", str(site), "run", "chq", "--no-deliver"]) == 0

    def full(*_a, **_k):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(runner, "build", full)
    assert main(["-s", str(site), "run", "chq", "--no-deliver", "--skip-sync"]) == 1
    with hist(site) as h:
        assert "The disk is full" in h.last_run("chq")["error"]
    assert (site / "data" / "cache.sqlite").is_file() and broken_copies(site) == []
    monkeypatch.undo()
    assert main(["-s", str(site), "run", "chq", "--no-deliver", "--skip-sync"]) == 0   # the cache still serves


def test_real_damage_is_still_set_aside(tmp_path):
    bad = tmp_path / "bad.sqlite"
    bad.write_bytes(b"this is not a database" * 100)
    with pytest.raises(sqlite3.DatabaseError) as err:
        sqlite3.connect(bad).execute("PRAGMA journal_mode=WAL")
    assert runner.cache_damaged(err.value)
    assert not runner.cache_damaged(sqlite3.OperationalError("database or disk is full"))
    assert not runner.cache_damaged(sqlite3.OperationalError("database is locked"))
    assert runner.cache_damaged(sqlite3.DatabaseError("database disk image is malformed"))


def test_two_quarantines_in_one_second_never_overwrite_each_other(site, monkeypatch):
    s = load_site(site)
    fixed = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(runner, "now_utc", lambda: fixed)
    moved = []
    for text in ("first", "second"):
        s.cache_path.parent.mkdir(parents=True, exist_ok=True)
        s.cache_path.write_text(text, encoding="utf-8")
        moved.append(runner.quarantine_cache(s))
    assert moved[0] != moved[1]
    assert [p.read_text(encoding="utf-8") for p in moved] == ["first", "second"]


# ------------------------------------------------------------------ TASK-battery-defaults
TASK_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def test_the_task_definition_runs_on_battery_and_starts_late(tmp_path):
    site = tmp_path / "my lib (main)"
    exe = r"C:\Program Files (x86)\fx\folio-exchange.exe"
    text = schedule.windows_task_xml(site, exe, "PC\\librarian")
    task = ET.fromstring(text.split("?>", 1)[1])
    setting = {e.tag.split("}")[1]: e.text for e in task.find("t:Settings", TASK_NS)}
    assert setting["DisallowStartIfOnBatteries"] == "false" and setting["StopIfGoingOnBatteries"] == "false"
    assert setting["StartWhenAvailable"] == "true" and setting["ExecutionTimeLimit"] == "PT12H"
    assert setting["MultipleInstancesPolicy"] == "IgnoreNew"
    trigger = task.find("t:Triggers/t:TimeTrigger", TASK_NS)
    assert trigger.find("t:StartBoundary", TASK_NS).text.endswith("T00:07:00")
    assert trigger.find("t:Repetition/t:Interval", TASK_NS).text == "PT1H"
    principal = task.find("t:Principals/t:Principal", TASK_NS)
    assert principal.find("t:UserId", TASK_NS).text == "PC\\librarian"
    assert principal.find("t:LogonType", TASK_NS).text == "Password"
    command, arguments = schedule.windows_task_action(site, exe)
    assert task.find("t:Actions/t:Exec/t:Command", TASK_NS).text == command == f'"{exe}"'
    assert task.find("t:Actions/t:Exec/t:Arguments", TASK_NS).text == arguments


def test_the_task_is_registered_from_the_definition_with_one_password_prompt(tmp_path):
    argv = schedule.windows_task_args(tmp_path, tmp_path / "task.xml", "PC\\librarian")
    assert argv[:4] == ["schtasks", "/Create", "/TN", schedule.task_name(tmp_path)]
    assert argv[argv.index("/XML") + 1] == str(tmp_path / "task.xml")
    assert argv[argv.index("/RU") + 1] == "PC\\librarian" and argv[argv.index("/RP") + 1] == "*"
    assert "/TR" not in argv and "/SC" not in argv and argv[-1] == "/F"


@pytest.mark.skipif(os.name != "nt", reason="Windows command line quoting")
def test_the_task_arguments_survive_windows_quoting(tmp_path):
    printer = tmp_path / "printer.py"
    printer.write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8")
    site = tmp_path / "my lib (main) 100%"
    _command, arguments = schedule.windows_task_action(site, r"C:\fx\folio-exchange.exe")
    line = f"{schedule.win_quote(sys.executable)} {schedule.win_quote(str(printer))} {arguments}"
    got = json.loads(subprocess.run(line, capture_output=True, text=True).stdout)
    assert got == ["--site", str(site.resolve()), "run", "--due"]


def test_a_stale_task_mentions_battery_power(tmp_path):
    with History(tmp_path / "history.sqlite") as h:
        feedops.record_due_start(h, datetime.now(timezone.utc) - timedelta(hours=9))
        feed = types.SimpleNamespace(enabled=True, schedule="Sun 02:15")
        status = feedops.task_status(h, [feed], datetime.now(timezone.utc))
    assert status["state"] == "stale" and "battery" in status["text"]


# ------------------------------------------------------------ SFTP-pin-breaks-on-new-key-type
def ed25519_key():
    import paramiko
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    pem = Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                                     serialization.NoEncryption())
    return paramiko.Ed25519Key.from_private_key(io.StringIO(pem.decode()))


@pytest.fixture(scope="module")
def host_keys():
    import paramiko

    return {"rsa": paramiko.RSAKey.generate(2048), "ed25519": ed25519_key()}


@contextmanager
def ssh_server(*keys):
    """An SSH server on this computer offering ``keys`` as host keys, accepting the password pw."""
    import paramiko

    class Server(paramiko.ServerInterface):
        def check_auth_password(self, username, password):
            return paramiko.AUTH_SUCCESSFUL if password == "pw" else paramiko.AUTH_FAILED

        def get_allowed_auths(self, username):
            return "password"

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    sock.settimeout(0.2)
    stop, transports = threading.Event(), []

    def serve():
        while not stop.is_set():
            try:
                conn, _addr = sock.accept()
            except OSError:
                continue
            t = paramiko.Transport(conn)
            t.set_log_channel("tests.sftp-server")       # the client's records stay apart from the test server's
            for k in keys:
                t.add_server_key(k)
            transports.append(t)
            try:
                t.start_server(server=Server())
            except Exception:  # noqa: BLE001 - a refused or mismatched client ends its own connection
                t.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield sock.getsockname()[1]
    finally:
        stop.set()
        thread.join(5)
        for t in transports:
            t.close()
        sock.close()


def pin(port, key, key_type=None):
    d = {"transport": "sftp", "host": "127.0.0.1", "port": port, "username": "u", "secret": "env:PW",
         "host_key_fingerprint": delivery.sha256_fingerprint(key)}
    return {**d, "host_key_type": key_type} if key_type else d


def test_a_pinned_rsa_key_still_works_when_the_server_adds_ed25519(host_keys, monkeypatch):
    monkeypatch.setenv("PW", "pw")
    rsa, ed = host_keys["rsa"], host_keys["ed25519"]
    with ssh_server(rsa, ed) as port:
        assert delivery.host_key_fingerprints("127.0.0.1", port)["type"] == "ssh-ed25519"   # the new key leads
        for d in (pin(port, rsa, "ssh-rsa"), pin(port, rsa), pin(port, ed, "ssh-ed25519"), pin(port, ed)):
            client = delivery._connect(d)
            assert delivery.sha256_fingerprint(client.get_transport().get_remote_server_key()) == \
                d["host_key_fingerprint"]
            client.close()


def test_the_pin_stays_strict(host_keys, monkeypatch):
    import paramiko

    monkeypatch.setenv("PW", "pw")
    other = paramiko.RSAKey.generate(2048)
    with ssh_server(host_keys["rsa"], host_keys["ed25519"]) as port:
        for d in (pin(port, other, "ssh-rsa"), pin(port, other)):
            with pytest.raises(paramiko.SSHException, match="Host key for .* expected "):
                delivery._connect(d)
        with pytest.raises(paramiko.SSHException):                     # a type the server does not have
            delivery._connect(pin(port, host_keys["rsa"], "ecdsa-sha2-nistp256"))


def test_an_rsa_pin_allows_the_sha2_signatures():
    off = delivery.disabled_host_key_algorithms("ssh-rsa")
    assert "rsa-sha2-256" not in off and "rsa-sha2-512" not in off and "ssh-ed25519" in off
    assert "ssh-ed25519" not in delivery.disabled_host_key_algorithms("ssh-ed25519")


def test_the_key_type_is_kept_with_the_fingerprint(site, keyring):
    s = load_site(site)
    library = RecipeLibrary(s.recipe_dirs)
    keys = {"sha256": "SHA256:" + "k" * 43, "md5": "aa:" * 15 + "aa", "type": "ssh-rsa"}
    v = setupops.feed_settings(s, library, "chq")["version"]
    form = {"transport": "sftp", "host": "sftp.vendor.example", "port": 22, "username": "lib1", "remote_dir": ".",
            "vendor_fingerprint": keys["sha256"]}
    out = setupops.save_delivery(s, library, "chq", form, version=v, read_keys=lambda h, p: keys)
    assert read_yaml(site / "feeds" / "chq.yaml")["delivery"]["host_key_type"] == "ssh-rsa"
    assert out["delivery"]["host_key_type"] == "ssh-rsa"
    problems = validate_recipe({"delivery": {"host_key_type": "ssh-dss"}}, "feed", shared=False)
    assert any("delivery.host_key_type" in p for p in problems)



# ---------------------------------------------------- SFTP-pin-reads-only-the-preferred-key
def delivery_form(port, **kw):
    return {"transport": "sftp", "host": "127.0.0.1", "port": port, "username": "u", "remote_dir": ".", **kw}


def test_every_offered_key_is_read(host_keys):
    rsa, ed = host_keys["rsa"], host_keys["ed25519"]
    with ssh_server(rsa, ed) as port:
        keys = delivery.host_key_fingerprints("127.0.0.1", port)
    assert keys["type"] == "ssh-ed25519" and keys["sha256"] == delivery.sha256_fingerprint(ed)
    assert [(k["type"], k["sha256"], k["md5"]) for k in keys["keys"]] == [
        ("ssh-ed25519", delivery.sha256_fingerprint(ed), delivery.md5_fingerprint(ed)),
        ("ssh-rsa", delivery.sha256_fingerprint(rsa), delivery.md5_fingerprint(rsa))]
    with ssh_server(rsa) as port:
        assert [k["type"] for k in delivery.host_key_fingerprints("127.0.0.1", port)["keys"]] == ["ssh-rsa"]


def test_the_vendors_rsa_fingerprint_is_pinned_when_the_server_leads_with_ed25519(site, keyring, host_keys,
                                                                                    monkeypatch):
    monkeypatch.setenv("PW", "pw")
    s = load_site(site)
    library = RecipeLibrary(s.recipe_dirs)
    rsa, ed = host_keys["rsa"], host_keys["ed25519"]
    with ssh_server(rsa, ed) as port:
        v = setupops.feed_settings(s, library, "chq")["version"]
        out = setupops.save_delivery(s, library, "chq", delivery_form(port, vendor_fingerprint=delivery.md5_fingerprint(rsa)),
                                     version=v)
        d = read_yaml(site / "feeds" / "chq.yaml")["delivery"]
        assert d["host_key_fingerprint"] == delivery.sha256_fingerprint(rsa) and d["host_key_type"] == "ssh-rsa"
        assert "host_key_confirmed" not in d and out["delivery"]["host_key_confirmed"]
        server_key = out["server_key"]
        assert server_key["type"] == "ssh-rsa" and server_key["sha256"] == d["host_key_fingerprint"]
        assert [k["type"] for k in server_key["keys"]] == ["ssh-ed25519", "ssh-rsa"]
        client = delivery._connect({**d, "secret": "env:PW"})
        assert client.get_transport().get_remote_server_key().get_name() == "ssh-rsa"
        client.close()
        # saving again without a vendor value keeps the confirmed RSA pin
        setupops.save_delivery(s, library, "chq", delivery_form(port, remote_dir="in"), version=out["version"])
        d = read_yaml(site / "feeds" / "chq.yaml")["delivery"]
        assert d["remote_dir"] == "in" and d["host_key_type"] == "ssh-rsa"
        assert d["host_key_fingerprint"] == delivery.sha256_fingerprint(rsa) and "host_key_confirmed" not in d


def test_a_fingerprint_matching_no_offered_key_lists_them_all(site, keyring, host_keys):
    import paramiko

    s = load_site(site)
    library = RecipeLibrary(s.recipe_dirs)
    rsa, ed = host_keys["rsa"], host_keys["ed25519"]
    path = site / "feeds" / "chq.yaml"
    before = path.read_bytes()
    v = setupops.feed_settings(s, library, "chq")["version"]
    other = delivery.sha256_fingerprint(paramiko.RSAKey.generate(2048))
    with ssh_server(rsa, ed) as port, pytest.raises(FxError) as err:
        setupops.save_delivery(s, library, "chq", delivery_form(port, vendor_fingerprint=other), version=v)
    text = str(err.value)
    assert "not the fingerprint you entered" not in text and "None of them is the fingerprint you entered" in text
    assert f"ssh-ed25519 {delivery.sha256_fingerprint(ed)}" in text and f"ssh-rsa {delivery.sha256_fingerprint(rsa)}" in text
    assert path.read_bytes() == before


def test_a_key_confirmed_later_can_be_another_offered_type(site, keyring, host_keys):
    s = load_site(site)
    library = RecipeLibrary(s.recipe_dirs)
    rsa, ed = host_keys["rsa"], host_keys["ed25519"]
    path = site / "feeds" / "chq.yaml"
    with ssh_server(rsa, ed) as port:
        v = setupops.feed_settings(s, library, "chq")["version"]
        out = setupops.save_delivery(s, library, "chq", delivery_form(port, confirm_later=True), version=v)
        raw = read_yaml(path)
        assert raw["enabled"] is False and raw["delivery"]["host_key_confirmed"] is False
        assert raw["delivery"]["host_key_fingerprint"] == delivery.sha256_fingerprint(ed)   # the preferred key
        assert len(out["server_key"]["keys"]) == 2
        with pytest.raises(FxError, match="or choose to confirm it later"):          # unconfirmed is not "known"
            setupops.save_delivery(s, library, "chq", delivery_form(port), version=out["version"])
        setupops.save_delivery(s, library, "chq", delivery_form(port, vendor_fingerprint=delivery.sha256_fingerprint(rsa)),
                               version=out["version"])
    d = read_yaml(path)["delivery"]
    assert d["host_key_fingerprint"] == delivery.sha256_fingerprint(rsa) and d["host_key_type"] == "ssh-rsa"
    assert "host_key_confirmed" not in d


def two_keys():
    rsa = {"sha256": "SHA256:" + "r" * 43, "md5": "aa:" * 15 + "aa", "type": "ssh-rsa"}
    ed = {"sha256": "SHA256:" + "e" * 43, "md5": "bb:" * 15 + "bb", "type": "ssh-ed25519"}
    return rsa, ed, {**ed, "keys": [ed, rsa]}


def wizard_answers(url, pin):
    return [url, "diku", "fx_reader", False,          # keep the FOLIO password in FOLIO_PASSWORD only
            None, None, [], [], False, False, "sftp", "h.example", None, "u", None, pin]


def test_the_wizard_pins_the_offered_key_the_person_picks(tmp_path, folio, keyring, monkeypatch):
    from sitekit import FOLIO_TEST_PASSWORD

    from folio_exchange.wizard import ScriptedPrompter, run_wizard

    monkeypatch.setenv("FOLIO_PASSWORD", FOLIO_TEST_PASSWORD)
    monkeypatch.setattr(delivery, "check_delivery", lambda d: [])
    rsa, ed, read = two_keys()
    root = tmp_path / "site"
    p = ScriptedPrompter(wizard_answers(folio.url, "yes:ssh-rsa") + ["", None, "none"])   # no SFTP password yet
    assert run_wizard(root, p, read_host_key=lambda h, port: read) == 0, "\n".join(p.transcript)
    text = "\n".join(p.transcript)
    assert "The server offers 2 keys" in text and f"ssh-rsa: {rsa['sha256']}" in text
    assert f"ssh-ed25519: {ed['sha256']}" in text
    d = read_yaml(root / "feeds" / "chq.yaml")["delivery"]
    assert d["host_key_fingerprint"] == rsa["sha256"] and d["host_key_type"] == "ssh-rsa"
    assert "host_key_confirmed" not in d


def test_the_wizard_stops_when_no_offered_key_matches(tmp_path, folio, keyring, monkeypatch):
    from sitekit import FOLIO_TEST_PASSWORD

    from folio_exchange.wizard import ScriptedPrompter, run_wizard

    monkeypatch.setenv("FOLIO_PASSWORD", FOLIO_TEST_PASSWORD)
    _rsa, _ed, read = two_keys()
    root = tmp_path / "site"
    p = ScriptedPrompter(wizard_answers(folio.url, "no"))
    assert run_wizard(root, p, read_host_key=lambda h, port: read) == 1
    feed = read_yaml(root / "feeds" / "chq.yaml")
    assert feed["delivery"]["transport"] == "none" and "host_key_fingerprint" not in feed["delivery"]


# ------------------------------------------------------------------ MOVE-absolute-run-dirs
def test_held_runs_survive_a_moved_site_folder(site, tmp_path):
    edit_site(site, runs={"keep_local_runs": 1})
    released(site)                                                  # run A, delivered
    make_feed(site, outputs={"items": {"delimiter": ","}})
    assert main(["-s", str(site), "run", "chq"]) == 2               # run B, held: the layout changed
    with hist(site) as h:
        a, b = h.last_delivered("chq"), h.last_run("chq")
    new = tmp_path / "moved" / "site"
    shutil.copytree(site, new)
    shutil.rmtree(site / "out")                                     # moved (the open log file stays behind)
    a_dir, b_dir = (new / "out" / "chq" / Path(r["run_dir"]).name for r in (a, b))
    assert main(["-s", str(new), "run", "chq"]) == 2                # run C, held too: nothing pruned
    assert a_dir.is_dir() and b_dir.is_dir()
    s = load_site(new)
    with History(s.history_path) as h:
        row = h.get(b["id"])
        assert ui.run_folder(s, row) == b_dir.resolve()
        assert h.run_by_dir(b_dir)["id"] == b["id"]
    assert main(["-s", str(new), "release", "chq", str(b_dir), "--yes"]) == 0
    with History(s.history_path) as h:
        assert h.get(b["id"])["delivered"]
    assert json.loads((b_dir / "run-report.json").read_text(encoding="utf-8"))["status"] == "success"


# ------------------------------------------------------------ ALERT-webhook-keyed-by-folder-name
def warnings_for(root) -> list[dict]:
    s = load_site(root)
    library = RecipeLibrary(s.recipe_dirs)
    feeds, errors = load_all(s, library)
    with History(s.history_path) as h:
        return feedops.site_warnings(s, library, feeds, errors, h, datetime.now(timezone.utc))


def test_an_alert_url_that_cannot_be_found_is_a_warning(site, keyring, monkeypatch):
    from folio_exchange import credentials

    edit_site(site, alerts={"on": "failure", "webhook": "keyring", "heartbeat": "env:FX_TEST_HEARTBEAT"})
    missing = [w for w in warnings_for(site) if w["code"] == "alert-secret-missing"]
    assert len(missing) == 2 and "alerts.webhook" in missing[0]["text"] and "alerts.heartbeat" in missing[1]["text"]
    assert f"({site.name})" in missing[0]["text"] and "FX_TEST_HEARTBEAT" in missing[1]["text"]
    keyring[(credentials.WEBHOOK_SERVICE, site.name)] = "https://hooks.example.org/x"
    monkeypatch.setenv("FX_TEST_HEARTBEAT", "https://hc.example.org/ping")
    assert not [w for w in warnings_for(site) if w["code"] == "alert-secret-missing"]
    renamed = site.with_name("renamed")
    site.rename(renamed)                                            # the keyring entry belongs to the old name
    assert [w["text"][:14] for w in warnings_for(renamed) if w["code"] == "alert-secret-missing"] == [
        "alerts.webhook"]


# ------------------------------------------------------- R-SFTP-1: a typed pin whose type the server dropped
def test_a_typed_pin_the_server_no_longer_offers_is_a_changed_key(host_keys, monkeypatch):
    import paramiko

    monkeypatch.setenv("PW", "pw")
    rsa, ed = host_keys["rsa"], host_keys["ed25519"]
    with ssh_server(ed) as port:                                    # the vendor moved to Ed25519 only
        d = pin(port, rsa, "ssh-rsa")
        with pytest.raises(paramiko.SSHException) as err:
            delivery._connect(d)
    text = delivery.plain_error(err.value, d)
    assert text.startswith("Host key for 127.0.0.1: the server no longer offers the pinned ssh-rsa key "
                           f"(pinned {delivery.sha256_fingerprint(rsa)}); it now offers ssh-ed25519 "
                           f"{delivery.sha256_fingerprint(ed)}")
    assert "ask the vendor" in text and "IncompatiblePeer" not in text


# ------------------------------------------------- R-SFTP-2: probing for key types logs no tracebacks
@pytest.fixture
def paramiko_errors():
    import logging

    records: list = []

    class Catch(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.ERROR:
                records.append(record)

    logger, handler = logging.getLogger("paramiko"), Catch()
    level = logger.level
    logger.setLevel(logging.WARNING)                               # as cli._setup_logging leaves it
    logger.addHandler(handler)
    yield records
    logger.removeHandler(handler)
    logger.setLevel(level)


def test_probing_for_key_types_logs_no_errors(host_keys, monkeypatch, paramiko_errors):
    import logging

    monkeypatch.setenv("PW", "pw")
    rsa, ed = host_keys["rsa"], host_keys["ed25519"]
    kinds: list = []
    real = delivery.disabled_host_key_algorithms
    monkeypatch.setattr(delivery, "disabled_host_key_algorithms", lambda kind: kinds.append(kind) or real(kind))
    before = logging.getLogger("paramiko.transport").level
    with ssh_server(rsa, ed) as port:
        client = delivery._connect(pin(port, rsa))                  # untyped: Ed25519 is offered first
        client.close()
        assert kinds == ["ssh-rsa"]                 # no second Ed25519 try, and RSA before the ECDSA kinds
        kinds.clear()
        keys = delivery.host_key_fingerprints("127.0.0.1", port)
    assert [k["type"] for k in keys["keys"]] == ["ssh-ed25519", "ssh-rsa"]
    assert kinds[0] == "ssh-rsa" and all(k.startswith("ecdsa") for k in kinds[1:])
    assert paramiko_errors == [] and logging.getLogger("paramiko.transport").level == before


# ----------------------------------------------- R-SFTP-3: check --delivery compares every offered key
def unconfirmed_feed(site, key_type="ssh-rsa"):
    path = site / "feeds" / "chq.yaml"
    raw = read_yaml(path)
    raw["enabled"] = False
    raw["delivery"] = {"transport": "sftp", "host": "sftp.vendor.example", "username": "lib1", "remote_dir": ".",
                       "host_key_fingerprint": "SHA256:" + "r" * 43, "host_key_type": key_type,
                       "host_key_confirmed": False}
    write_yaml(path, raw)


def test_check_delivery_finds_an_unconfirmed_pin_among_the_offered_keys(site, keyring, capsys, monkeypatch):
    rsa, ed, read = two_keys()
    monkeypatch.setattr(delivery, "host_key_fingerprints", lambda host, port=22, timeout=30: read)
    unconfirmed_feed(site)
    assert main(["-s", str(site), "check", "--delivery", "chq"]) == 1
    out = capsys.readouterr().out
    assert "The server offers it as its ssh-rsa key (the same)." in out and "DIFFERENT" not in out
    other = {**ed, "sha256": "SHA256:" + "x" * 43}
    monkeypatch.setattr(delivery, "host_key_fingerprints",
                        lambda host, port=22, timeout=30: {**other, "keys": [other]})
    assert main(["-s", str(site), "check", "--delivery", "chq"]) == 1
    out = capsys.readouterr().out
    assert "DIFFERENT: ask the vendor" in out and f"ssh-ed25519: {other['sha256']}" in out


# --------------------------------------------- R-HIST-1: two programs open an old history file at once
def test_an_old_history_file_opened_at_once_gains_its_columns_once(tmp_path):
    failures: list = []
    for trial in range(60):
        path = tmp_path / f"history{trial}.sqlite"
        db = sqlite3.connect(path)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, feed TEXT NOT NULL, recipe TEXT,
            recipe_version TEXT, trigger TEXT, run_dir TEXT, started TEXT NOT NULL, finished TEXT,
            status TEXT NOT NULL DEFAULT 'running', items INTEGER, bibs INTEGER, delivered INTEGER NOT NULL DEFAULT 0,
            files TEXT, destinations TEXT, error TEXT, report TEXT, actor TEXT, destination TEXT)""")
        db.commit()
        db.close()
        start = threading.Barrier(6)

        def open_it(path=path, start=start):
            start.wait()
            try:
                History(path).close()
            except Exception as exc:  # noqa: BLE001 - every failure is the finding
                failures.append(repr(exc))

        threads = [threading.Thread(target=open_it) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with History(path) as h:
            assert "folio_source" in {r[1] for r in h.db.execute("PRAGMA table_info(runs)")}
    assert failures == []
