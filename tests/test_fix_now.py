"""Regression tests for the "fix now" findings of the 27 October 2026 review (docs/review-2026-10-27.md)."""
import sys
import types

import pytest
import yaml
from sitekit import FOLIO_TEST_PASSWORD, make_feed, make_site, write_yaml

from folio_exchange import client as client_mod
from folio_exchange import delivery
from folio_exchange.cli import main
from folio_exchange.client import FolioError, probe
from folio_exchange.errors import ConfigError
from folio_exchange.wizard import Prompter, ScriptedPrompter, run_wizard


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)


def fake_keyring(monkeypatch):
    store = {}
    monkeypatch.setitem(sys.modules, "keyring", types.SimpleNamespace(
        get_password=lambda s, u: store.get((s, u)),
        set_password=lambda s, u, v: store.__setitem__((s, u), v),
        get_keyring=lambda: types.SimpleNamespace(priority=5)))
    return store


def new_site_answers(url, *, transport=("local", "drop"), schedule=None, alerts="none"):
    """Answers for a fresh init with FOLIO_PASSWORD set and no keyring, up to the end."""
    return [url, "diku", "fx_reader", None, None, [], [], False, False, *transport, schedule, alerts]


def rerun_answers(*, resume=None, transport=(None, None)):
    """Answers for re-running init on an existing site and feed (connection kept, every default kept)."""
    answers = [False, None, None, None, None, None, False, *transport, None]
    if resume is not None:
        answers.append(resume)
    return answers + ["none"]


# ------------------------------------------------------------------ login and permissions
def test_a_busy_gateway_at_login_is_retried_and_never_a_traceback(site, folio, capsys):
    folio.fail_login = [503, 503]
    assert main(["-s", str(site), "check"]) == 0                     # retried, then logged in
    folio.fail_login = [503] * 10
    assert main(["-s", str(site), "check"]) == 1
    err = capsys.readouterr().err
    assert "gateway may be busy" in err and "Traceback" not in err and "_Transient" not in err


def test_probe_tells_refusals_from_other_failures():
    class Fake:
        def count(self, path):
            if path == "/loan-types":
                raise FolioError("GET /loan-types -> 403: forbidden", status=403)
            if path == "/locations":
                raise FolioError("GET /locations -> 404: no route", status=404)
            return 3

        def get(self, path, params=None):
            return {}

    rows = {r["name"]: r for r in probe(Fake(), ["instances", "marc"])}
    assert rows["loan_types"]["status"] == "denied"
    assert rows["locations"]["status"] == "failed" and "404" in rows["locations"]["error"]
    assert rows["instances"]["status"] == "ok" and rows["instances"]["count"] == 3
    assert rows["marc"]["status"] == "ok" and rows["marc"]["count"] is None


# ------------------------------------------------------------------ setup safe to re-run
def test_a_refused_password_is_never_stored(tmp_path, folio, monkeypatch):
    store = fake_keyring(monkeypatch)
    p = ScriptedPrompter([folio.url, "diku", "fx_reader", "wrong-password"])
    assert run_wizard(tmp_path / "site", p) == 1
    assert store == {} and not (tmp_path / "site" / "site.yaml").exists()
    assert "no password was stored" in "\n".join(p.transcript)
    p = ScriptedPrompter([folio.url, "diku", "fx_reader", FOLIO_TEST_PASSWORD, None, None, [], [], False, False,
                          "local", "drop", None, "none"])
    assert run_wizard(tmp_path / "site", p) == 0, "\n".join(p.transcript)
    assert store[("folio-exchange/folio", "fx_reader@diku")] == FOLIO_TEST_PASSWORD


def test_init_refuses_to_overwrite_files_it_cannot_read(tmp_path, folio, monkeypatch):
    monkeypatch.setenv("FOLIO_PASSWORD", FOLIO_TEST_PASSWORD)
    root = tmp_path / "site"
    assert run_wizard(root, ScriptedPrompter(new_site_answers(folio.url))) == 0
    feed = root / "feeds" / "chq.yaml"
    broken = feed.read_text(encoding="utf-8").replace("schedule: weekly", "schedule: [weekly")
    feed.write_text(broken, encoding="utf-8")
    p = ScriptedPrompter([False, None, None])
    assert run_wizard(root, p) == 1
    assert feed.read_text(encoding="utf-8") == broken
    assert "init stopped without changing it" in "\n".join(p.transcript)
    site_file = root / "site.yaml"
    site_file.write_text(site_file.read_text(encoding="utf-8") + "folio: [\n", encoding="utf-8")
    before = site_file.read_text(encoding="utf-8")
    assert run_wizard(root, ScriptedPrompter([])) == 1
    assert site_file.read_text(encoding="utf-8") == before


def test_init_reads_site_yaml_the_normal_way(tmp_path, folio, monkeypatch):
    monkeypatch.setenv("FOLIO_PASSWORD", FOLIO_TEST_PASSWORD)
    monkeypatch.setenv("FX_TEST_URL", folio.url)
    root = make_site(tmp_path / "site", "${FX_TEST_URL}")
    p = ScriptedPrompter([False, None, None, [], [], False, False, "local", "drop", None, "none"])
    assert run_wizard(root, p) == 0, "\n".join(p.transcript)
    assert "${FX_TEST_URL}" in (root / "site.yaml").read_text(encoding="utf-8")      # left as written


def test_rerunning_init_keeps_a_paused_feed_paused_and_keeps_comments(tmp_path, folio, monkeypatch):
    monkeypatch.setenv("FOLIO_PASSWORD", FOLIO_TEST_PASSWORD)
    root = tmp_path / "site"
    assert run_wizard(root, ScriptedPrompter(new_site_answers(folio.url, transport=("local", "drop")))) == 0
    feed = root / "feeds" / "chq.yaml"
    feed.write_text(feed.read_text(encoding="utf-8") + "# paused at the vendor's request, 27 Oct\n", encoding="utf-8")
    assert main(["-s", str(root), "feeds", "pause", "chq"]) == 0
    p = ScriptedPrompter(rerun_answers(resume=False))
    assert run_wizard(root, p) == 0, "\n".join(p.transcript)
    assert yaml.safe_load(feed.read_text(encoding="utf-8"))["enabled"] is False
    assert "paused at the vendor's request" in (root / "feeds" / "chq.yaml.bak").read_text(encoding="utf-8")


def test_a_feed_paused_only_by_init_is_resumed_without_asking(tmp_path, folio, monkeypatch):
    monkeypatch.setenv("FOLIO_PASSWORD", FOLIO_TEST_PASSWORD)
    root = tmp_path / "site"
    p = ScriptedPrompter([folio.url, "diku", "fx_reader", None, None, [], [], False, False, "sftp", "h.example",
                          None, "u", None, "no"])
    assert run_wizard(root, p, read_host_key=lambda h, port: "SHA256:" + "k" * 43) == 1
    p = ScriptedPrompter(rerun_answers(transport=("local", "drop")))
    assert run_wizard(root, p) == 0, "\n".join(p.transcript)
    assert yaml.safe_load((root / "feeds" / "chq.yaml").read_text(encoding="utf-8"))["enabled"] is True


def test_blank_secrets_mean_later_and_no_keyring_means_an_env_reference(tmp_path, folio, monkeypatch):
    monkeypatch.setenv("FOLIO_PASSWORD", FOLIO_TEST_PASSWORD)
    root = tmp_path / "site"
    answers = new_site_answers(folio.url)[:-1] + ["email", "ops@lib.example", "fx@lib.example", "smtp.lib.example",
                                                   None, "mailer", "FX_MAIL", False]
    p = ScriptedPrompter(answers)
    assert run_wizard(root, p) == 0, "\n".join(p.transcript)
    email = yaml.safe_load((root / "site.yaml").read_text(encoding="utf-8"))["alerts"]["email"]
    assert email["username"] == "mailer" and email["secret"] == "env:FX_MAIL"


def test_an_unconfirmed_host_key_never_logs_in(tmp_path, folio, monkeypatch):
    monkeypatch.setenv("FOLIO_PASSWORD", FOLIO_TEST_PASSWORD)
    called = []
    monkeypatch.setattr(delivery, "check_delivery", lambda d: called.append(d) or [])
    root = tmp_path / "site"
    p = ScriptedPrompter([folio.url, "diku", "fx_reader", None, None, [], [], False, False, "sftp", "h.example",
                          None, "u", None, "unconfirmed", None, None, "none"])
    assert run_wizard(root, p, read_host_key=lambda h, port: "SHA256:" + "k" * 43) == 0, "\n".join(p.transcript)
    feed = yaml.safe_load((root / "feeds" / "chq.yaml").read_text(encoding="utf-8"))
    assert feed["delivery"]["host_key_confirmed"] is False and feed["enabled"] is False
    assert called == []
    with pytest.raises(ConfigError, match="has not been confirmed"):
        delivery.deliver([], {**feed["delivery"], "secret": "env:X"}, "chq")


def test_end_of_input_stops_the_wizard_instead_of_looping(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": (_ for _ in ()).throw(EOFError()))
    with pytest.raises(KeyboardInterrupt):
        Prompter().ask("FOLIO API address", None, lambda a: "Enter the full address")


def test_init_uses_folio_exchange_site(tmp_path, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr("folio_exchange.wizard.run_wizard", lambda root, p: seen.append(root) or 0)
    monkeypatch.setenv("FOLIO_EXCHANGE_SITE", str(tmp_path / "from-env"))
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    assert seen[-1] == (tmp_path / "from-env").resolve()
    assert main(["-s", str(tmp_path / "a"), "init", str(tmp_path / "b")]) == 1


def test_a_second_feed_on_the_same_recipe_gets_its_own_file_names(tmp_path, folio, monkeypatch):
    monkeypatch.setenv("FOLIO_PASSWORD", FOLIO_TEST_PASSWORD)
    root = tmp_path / "site"
    assert run_wizard(root, ScriptedPrompter(new_site_answers(folio.url))) == 0
    p = ScriptedPrompter([False, None, "chq-sci", [], [], False, False, "local", "drop", None, "none"])
    assert run_wizard(root, p) == 0, "\n".join(p.transcript)
    assert yaml.safe_load((root / "feeds" / "chq-sci.yaml").read_text(encoding="utf-8"))["file_prefix"] == "chq-sci"
    assert write_yaml and make_feed                                   # helpers used by later sections


# ------------------------------------------------------------------ deliveries
PIN = {"transport": "sftp", "host": "h", "username": "u", "host_key_fingerprint": "SHA256:x", "secret": "env:PW"}


def two_files(tmp_path):
    a, b = tmp_path / "a.mrc", tmp_path / "b.psv"
    a.write_text("a")
    b.write_text("b")
    return a, b


def test_a_failed_staged_upload_removes_its_temporary_files(tmp_path, monkeypatch):
    from unittest import mock

    from folio_exchange.errors import FxError

    monkeypatch.setenv("PW", "pw")
    a, b = two_files(tmp_path)
    with mock.patch("paramiko.SSHClient") as ssh:
        sftp = ssh.return_value.open_sftp.return_value
        sftp.put.side_effect = [None, OSError("connection lost")]
        with pytest.raises(FxError) as err:
            delivery.deliver([a, b], PIN, "chq")
    assert [c.args[0] for c in sftp.remove.call_args_list] == ["./a.mrc.part", "./b.psv.part"]
    assert "No file was put in place" in str(err.value) and "may remain" not in str(err.value)
    with mock.patch("paramiko.SSHClient") as ssh:
        sftp = ssh.return_value.open_sftp.return_value
        sftp.put.side_effect = [None, OSError("connection lost")]
        sftp.remove.side_effect = OSError("gone")
        with pytest.raises(FxError, match="may remain there: a.mrc.part, b.psv.part"):
            delivery.deliver([a, b], PIN, "chq")


def test_sftp_sessions_time_out_instead_of_hanging(tmp_path, monkeypatch):
    from unittest import mock

    monkeypatch.setenv("PW", "pw")
    a, _b = two_files(tmp_path)
    with mock.patch("paramiko.SSHClient") as ssh:
        delivery.deliver([a], PIN, "chq")
    ssh.return_value.get_transport.return_value.set_keepalive.assert_called_once_with(30)
    channel = ssh.return_value.open_sftp.return_value.get_channel.return_value
    channel.settimeout.assert_called_once_with(delivery.SFTP_TIMEOUT)


def test_the_pinned_key_is_compared_exactly(monkeypatch):
    from unittest import mock

    import paramiko

    monkeypatch.setenv("PW", "pw")
    key = mock.Mock()
    key.asbytes.return_value = b"server-key"
    good = delivery.sha256_fingerprint(key)
    with mock.patch("paramiko.SSHClient") as ssh:
        delivery._connect({**PIN, "host_key_fingerprint": good})
        policy = ssh.return_value.set_missing_host_key_policy.call_args.args[0]
    policy.missing_host_key(None, "h", key)                          # matches: accepted
    with mock.patch("paramiko.SSHClient") as ssh:
        delivery._connect({**PIN, "host_key_fingerprint": good.lower()})
        policy = ssh.return_value.set_missing_host_key_policy.call_args.args[0]
    with pytest.raises(paramiko.SSHException, match="expected"):
        policy.missing_host_key(None, "h", key)


def test_release_will_not_send_an_older_run_when_a_newer_one_failed(site, monkeypatch):
    from folio_exchange.errors import FxError

    assert main(["-s", str(site), "run", "chq"]) == 2                 # held
    assert main(["-s", str(site), "release", "chq", "--yes"]) == 0
    assert main(["-s", str(site), "build", "chq"]) == 0               # an older, built run

    def broken(files, d, name):
        raise FxError("Delivery failed (OSError: drive gone). No file was put in place at the destination.")

    monkeypatch.setattr("folio_exchange.delivery.deliver", broken)
    assert main(["-s", str(site), "run", "chq"]) == 1                 # the newest run failed
    monkeypatch.undo()
    assert main(["-s", str(site), "release", "chq", "--yes"]) == 1


def test_two_feeds_cannot_overwrite_each_other_at_one_destination(site, capsys):
    make_feed(site, name="sci", filters={"include_location_codes": ["SCI-STACKS"]})
    assert main(["-s", str(site), "feeds"]) == 1
    out = capsys.readouterr().out
    assert "one would overwrite the other" in out and "file_prefix: sci" in out
    make_feed(site, name="sci", file_prefix="sci", filters={"include_location_codes": ["SCI-STACKS"]})
    assert main(["-s", str(site), "feeds"]) == 0
