"""Unit tests ported from folio-chq-extract: dates, MARC, SFTP delivery, the client, credentials."""
import sys
import types
from io import BytesIO
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from pymarc import Field, Indicators, MARCReader, Record, Subfield

from folio_exchange import credentials, delivery
from folio_exchange.dates import fmt, norm
from folio_exchange.errors import ConfigError, DeliveryError, FxError
from folio_exchange.marc import MarcBuilder, _set_instance_link

PIN = {"host": "h", "username": "u", "host_key_fingerprint": "SHA256:x"}


def test_norm_dates():
    assert norm("2024-03-01T23:30:00.000-05:00") == "2024-03-02T04:30:00"
    assert norm("2024-03-01") == "2024-03-01T00:00:00"
    assert norm("not a date") is None
    assert fmt("2024-03-02T04:30:00Z", "%Y-%m-%d", ZoneInfo("America/New_York")) == "2024-03-01"


def test_legacy_dates_keep_their_day_in_local_timezone():
    tz = ZoneInfo("America/Chicago")
    assert fmt(norm("3/15/1998", tz), "%Y-%m-%d", tz) == "1998-03-15"
    assert fmt(norm("1998-03-15", tz), "%Y-%m-%d", tz) == "1998-03-15"


def test_long_field_is_truncated_and_readable():
    rec = Record(leader="00000nam a2200000 a 4500")
    rec.add_field(Field(tag="001", data="x"))
    rec.add_field(Field(tag="245", indicators=Indicators("0", "0"), subfields=[Subfield("a", "T")]))
    rec.add_field(Field(tag="505", indicators=Indicators("0", " "), subfields=[Subfield("a", "é" * 8000)]))
    mb = MarcBuilder({"embed_items": True, "item_field": {"tag": "949", "subfields": {"a": "barcode"}}},
                     lambda n, v: v)
    data = mb.finalize(rec, {"hrid": "in1"}, [{"barcode": "B1"}])
    assert mb.long_fields == 1
    out = next(MARCReader(BytesIO(data), to_unicode=True, force_utf8=True))
    assert out is not None and out["001"].data == "in1" and out["949"]["a"] == "B1"
    assert len(out["505"]["a"].encode()) < 9999


def test_oversize_bib_sheds_embedded_items():
    rec = Record(leader="00000nam a2200000 a 4500")
    rec.add_field(Field(tag="245", indicators=Indicators("0", "0"), subfields=[Subfield("a", "T")]))
    mb = MarcBuilder({"embed_items": True, "item_field": {"tag": "949", "max_per_record": 5000,
                                                          "subfields": {"a": "barcode", "c": "call_number"}}},
                     lambda n, v: v)
    rows = [{"barcode": f"B{i:06d}", "call_number": "X" * 200} for i in range(1000)]
    data = mb.finalize(rec, {"hrid": "in1", "id": "uuid"}, rows)
    assert data is not None and len(data) <= 99999 and mb.truncated == 1


def test_instance_link_is_set_and_keeps_other_999_subfields():
    rec = Record()
    rec.add_field(Field(tag="999", indicators=Indicators("f", "f"),
                        subfields=[Subfield("i", "wrong"), Subfield("s", "srs-id")]))
    assert _set_instance_link(rec, "inst-uuid") is False
    assert [(s.code, s.value) for s in rec["999"].subfields] == [("i", "inst-uuid"), ("s", "srs-id")]
    assert _set_instance_link(rec, "inst-uuid") is True

    bare = Record()
    bare.add_field(Field(tag="999", indicators=Indicators(" ", " "), subfields=[Subfield("a", "local")]))
    assert _set_instance_link(bare, "inst-uuid") is False
    links = [f for f in bare.get_fields("999") if f.indicator1 == "f"]
    assert len(links) == 1 and links[0]["i"] == "inst-uuid"
    assert len(bare.get_fields("999")) == 2


def test_fingerprint_format():
    key = mock.Mock()
    key.asbytes.return_value = b"key-bytes"
    assert delivery.sha256_fingerprint(key).startswith("SHA256:")


def test_sftp_uploads_atomically(tmp_path, monkeypatch):
    monkeypatch.setenv("SFTP_PW", "pw")
    f = tmp_path / "x.csv"
    f.write_text("a")
    with mock.patch("paramiko.SSHClient") as ssh:
        sftp = ssh.return_value.open_sftp.return_value
        out = delivery.deliver([f], {**PIN, "transport": "sftp", "remote_dir": "/in", "secret": "env:SFTP_PW"}, "x")
    sftp.put.assert_called_once_with(str(f), "/in/x.csv.part", confirm=True)
    sftp.posix_rename.assert_called_once_with("/in/x.csv.part", "/in/x.csv")
    assert out == ["sftp://u@h:22//in/x.csv"]
    assert ssh.return_value.connect.call_args.kwargs["password"] == "pw"


def test_sftp_renames_only_after_all_uploads(tmp_path, monkeypatch):
    monkeypatch.setenv("SFTP_PW", "pw")
    a, b = tmp_path / "a.mrc", tmp_path / "b.csv"
    a.write_text("a")
    b.write_text("b")
    with mock.patch("paramiko.SSHClient") as ssh:
        sftp = ssh.return_value.open_sftp.return_value
        sftp.put.side_effect = [None, OSError("connection lost")]
        with pytest.raises(FxError, match="No file was put in place") as err:
            delivery.deliver([a, b], {**PIN, "transport": "sftp", "secret": "env:SFTP_PW"}, "x")
    sftp.posix_rename.assert_not_called()
    assert not isinstance(err.value, DeliveryError)


def test_direct_mode_login_failure_is_not_a_partial_delivery(tmp_path, monkeypatch):
    import paramiko

    monkeypatch.setenv("SFTP_PW", "pw")
    (tmp_path / "a.mrc").write_text("a")
    with mock.patch("paramiko.SSHClient") as ssh:
        ssh.return_value.connect.side_effect = paramiko.SSHException("Host key for h is SHA256:y, expected x")
        with pytest.raises(FxError, match="No file was put in place") as err:
            delivery.deliver([tmp_path / "a.mrc"], {**PIN, "transport": "sftp", "upload_mode": "direct",
                                                    "secret": "env:SFTP_PW"}, "x")
    assert not isinstance(err.value, DeliveryError)


def test_local_delivery_stopping_part_way_is_a_partial_delivery(tmp_path, monkeypatch):
    src = tmp_path / "run"
    src.mkdir()
    for n in ("a.mrc", "b.psv"):
        (src / n).write_text(n)
    real = __import__("os").replace
    calls = []

    def flaky(a, b):
        calls.append(b)
        if len(calls) == 2:
            raise PermissionError("in use")
        return real(a, b)

    monkeypatch.setattr("folio_exchange.delivery.os.replace", flaky)
    with pytest.raises(DeliveryError) as err:
        delivery.deliver([src / "a.mrc", src / "b.psv"], {"transport": "local", "directory": str(tmp_path / "d")},
                         "f")
    assert len(err.value.sent) == 1 and err.value.sent[0].endswith("a.mrc")
    assert not list((tmp_path / "d").glob("*.part"))


def test_sftp_direct_mode_uploads_to_final_names_without_stat_or_rename(tmp_path, monkeypatch):
    monkeypatch.setenv("SFTP_PW", "pw")
    a, b = tmp_path / "a.mrc", tmp_path / "b.psv"
    a.write_text("a")
    b.write_text("b")
    with mock.patch("paramiko.SSHClient") as ssh:
        sftp = ssh.return_value.open_sftp.return_value
        out = delivery.deliver([a, b], {**PIN, "transport": "sftp", "remote_dir": "in", "upload_mode": "direct",
                                        "secret": "env:SFTP_PW"}, "x")
    assert out == ["sftp://u@h:22/in/a.mrc", "sftp://u@h:22/in/b.psv"]
    assert sftp.put.call_args_list == [mock.call(str(a), "in/a.mrc", confirm=False),
                                       mock.call(str(b), "in/b.psv", confirm=False)]
    assert not sftp.rename.called and not sftp.posix_rename.called and not sftp.remove.called


def test_direct_mode_failure_reports_what_the_vendor_already_has(tmp_path, monkeypatch):
    monkeypatch.setenv("SFTP_PW", "pw")
    a, b = tmp_path / "a.mrc", tmp_path / "b.psv"
    a.write_text("a")
    b.write_text("b")
    with mock.patch("paramiko.SSHClient") as ssh:
        ssh.return_value.open_sftp.return_value.put.side_effect = [None, OSError("connection lost")]
        with pytest.raises(DeliveryError) as err:
            delivery.deliver([a, b], {**PIN, "transport": "sftp", "upload_mode": "direct",
                                      "secret": "env:SFTP_PW"}, "chq")
    assert err.value.sent == ["sftp://u@h:22/./a.mrc"]
    assert "a.mrc" in str(err.value) and "run chq" in str(err.value)


def test_sftp_check_lists_without_writing(monkeypatch):
    monkeypatch.setenv("SFTP_PW", "pw")
    with mock.patch("paramiko.SSHClient") as ssh:
        sftp = ssh.return_value.open_sftp.return_value
        sftp.listdir.return_value = ["b.psv", "a.mrc"]
        names = delivery.check_delivery({**PIN, "transport": "sftp", "remote_dir": "/in/", "secret": "env:SFTP_PW"})
    assert names == ["a.mrc", "b.psv"]
    sftp.listdir.assert_called_once_with("/in")
    assert not sftp.put.called and not sftp.remove.called and not sftp.rename.called
    ssh.return_value.close.assert_called_once()


def test_sftp_check_tolerates_upload_only_account(monkeypatch):
    monkeypatch.setenv("SFTP_PW", "pw")
    with mock.patch("paramiko.SSHClient") as ssh:
        ssh.return_value.open_sftp.return_value.listdir.side_effect = PermissionError(13, "Permission denied")
        assert delivery.check_delivery({**PIN, "transport": "sftp", "secret": "env:SFTP_PW"}) is None


def test_sftp_without_any_password_says_how_to_store_one():
    with pytest.raises(ConfigError, match="secret set"):
        delivery.check_delivery({**PIN, "transport": "sftp", "secret": "keyring"})


def test_check_delivery_command_reports_failure(site, capsys, monkeypatch):
    from sitekit import make_feed

    from folio_exchange.cli import main

    make_feed(site, delivery={"transport": "sftp", "host": "h", "username": "u",
                              "host_key_fingerprint": "SHA256:" + "x" * 43, "directory": None})
    with mock.patch("folio_exchange.delivery.check_delivery", side_effect=OSError("refused")):
        assert main(["-s", str(site), "check", "--sftp"]) == 1
    assert "FAILED   OSError: refused" in capsys.readouterr().out


def test_local_delivery_copies_then_renames(tmp_path):
    src = tmp_path / "run"
    src.mkdir()
    (src / "a.mrc").write_bytes(b"x")
    out = delivery.deliver([src / "a.mrc"], {"transport": "local", "directory": str(tmp_path / "drop")}, "f")
    assert (tmp_path / "drop" / "a.mrc").read_bytes() == b"x"
    assert not list((tmp_path / "drop").glob("*.part"))
    assert out[0].startswith("file:")


def test_prune_keeps_protected_runs(tmp_path):
    for name in ("20260101-000000", "20260102-000000", "20260103-000000", "preview-1", "preview-2",
                 "preview-3", "preview-4"):
        (tmp_path / name).mkdir()
    delivery.prune_runs(tmp_path, 1, {tmp_path / "20260101-000000"})
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == ["20260101-000000", "20260103-000000", "preview-2", "preview-3", "preview-4"]


def test_login_retries_transient_errors(monkeypatch):
    from folio_exchange import client as cm

    monkeypatch.setattr(cm.time, "sleep", lambda s: None)
    c = cm.FolioClient("http://folio", "t", "u", "p")
    calls = {"n": 0}

    def fake_login():
        calls["n"] += 1
        if calls["n"] == 1:
            raise cm._TransientLoginError("502")
        c._token = "tok"

    monkeypatch.setattr(c, "login", fake_login)
    resp = mock.Mock(status_code=200, ok=True, content=b"{}", json=lambda: {"ok": 1})
    monkeypatch.setattr(c.session, "request", lambda *a, **k: resp)
    assert c.get("/locations") == {"ok": 1}
    assert calls["n"] == 2


def test_credentials_env_then_keyring(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", types.SimpleNamespace(get_password=lambda s, u: f"{s}:{u}"))
    monkeypatch.setenv("MY_PW", "from-env")
    assert credentials.resolve("env:MY_PW", "svc", "u") == "from-env"
    assert credentials.resolve("keyring", "svc", "u") == "svc:u"
    assert credentials.resolve(None, "svc", "u") == "svc:u"
    assert credentials.resolve("keyring", "svc", None) is None
    monkeypatch.setenv("FOLIO_PASSWORD", "first")
    assert credentials.resolve("keyring", "svc", "u", env_first="FOLIO_PASSWORD") == "first"
    assert credentials.source("keyring", "svc", "u", env_first="FOLIO_PASSWORD") == "environment (FOLIO_PASSWORD)"
    assert credentials.source("keyring", "svc", "u") == "system keyring (svc, u)"
    assert credentials.source("env:NOPE", "svc", "u").startswith("NOT FOUND")

    def broken(_s, _u):
        raise RuntimeError("no backend")

    monkeypatch.setitem(sys.modules, "keyring", types.SimpleNamespace(get_password=broken))
    assert credentials.resolve("keyring", "svc", "u") is None
    assert credentials.source("keyring", "svc", "u").startswith("NOT FOUND")


def test_credentials_without_keyring_installed():
    assert credentials.resolve("keyring", "svc", "u") is None       # the autouse fixture removes keyring
    assert "not installed" in credentials.source("keyring", "svc", "u")
    with pytest.raises(Exception, match="No system keyring"):
        credentials.store("svc", "u", "value")
