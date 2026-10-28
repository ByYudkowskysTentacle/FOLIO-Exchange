"""Small hardening (SEC-3, SEC-4, SEC-6): trusting a campus certificate, the cache keeping no staff data, and
warning when other users can change FOLIO-Exchange's folders."""
import datetime as dt
import ipaddress
import os
import ssl
import subprocess
import sys

import pytest
from mock_folio import MockFolio, U, default_data
from sitekit import FOLIO_TEST_PASSWORD, make_site

from folio_exchange import permissions
from folio_exchange.cache import Cache, unpack
from folio_exchange.client import FolioClient, FolioUnreachable, _ContextAdapter, tls_verify
from folio_exchange.config import load_site
from folio_exchange.errors import ConfigError
from folio_exchange.sync import Syncer
from folio_exchange.wizard import ScriptedPrompter, run_wizard

# ---------------------------------------------------------------- SEC-4: certificates


def _certificates(folder):
    """A private CA and a certificate for 127.0.0.1 that it signed, as a campus TLS proxy would have."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = dt.datetime.now(dt.timezone.utc)
    ca_key, key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Campus CA")])
    ca_ski = x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key())
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
          .not_valid_after(now + dt.timedelta(days=30))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                       content_commitment=False, key_encipherment=False, data_encipherment=False,
                                       key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
          .add_extension(ca_ski, critical=False)
          .sign(ca_key, hashes.SHA256()))
    leaf = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                           critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski), critical=False)
            .sign(ca_key, hashes.SHA256()))
    pem = serialization.Encoding.PEM
    (folder / "campus-ca.pem").write_bytes(ca.public_bytes(pem))
    (folder / "server.pem").write_bytes(leaf.public_bytes(pem))
    (folder / "server.key").write_bytes(key.private_bytes(pem, serialization.PrivateFormat.PKCS8,
                                                          serialization.NoEncryption()))
    return folder / "campus-ca.pem"


@pytest.fixture
def https_folio(tmp_path):
    """The mock FOLIO behind TLS, with a certificate from a private CA that the usual bundle does not trust."""
    certs = tmp_path / "certs"
    certs.mkdir()
    ca = _certificates(certs)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certs / "server.pem", certs / "server.key")
    mock = MockFolio(default_data())
    mock.server.socket = ctx.wrap_socket(mock.server.socket, server_side=True)
    mock.url = mock.url.replace("http://", "https://")
    with mock:
        yield mock, ca


def _client(url, verify):
    return FolioClient(url, "diku", "fx_reader", FOLIO_TEST_PASSWORD, max_retries=0, verify_tls=verify)


def test_a_ca_bundle_relative_to_the_site_folder_is_trusted(https_folio, tmp_path):
    mock, ca = https_folio
    root = tmp_path / "site"
    (root / "certs").mkdir(parents=True)
    (root / "certs" / "campus.pem").write_bytes(ca.read_bytes())
    verify = tls_verify({"verify_tls": True, "ca_bundle": "certs/campus.pem"}, root)
    assert verify == str((root / "certs" / "campus.pem").resolve())
    client = _client(mock.url, verify)
    assert client.session.verify == verify
    client.connect()
    assert client.get("/locations")["locations"]


def test_a_ca_bundle_wins_over_the_environments_bundle(https_folio, tmp_path, monkeypatch):
    mock, ca = https_folio
    import certifi
    for name in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):    # IT set a bundle that does not hold the campus CA
        monkeypatch.setenv(name, certifi.where())
    client = _client(mock.url, tls_verify({"verify_tls": True, "ca_bundle": str(ca)}, None))
    client.connect()
    assert client.get("/locations")["locations"]
    assert _client(mock.url, False).session.merge_environment_settings(mock.url, {}, None, None, None)[
        "verify"] is False                                      # verify_tls: false is not turned back on either
    # with nothing in site.yaml the environment's bundle still applies, as it does for any requests program
    default = _client(mock.url, tls_verify({"verify_tls": True}, None)).session
    assert default.merge_environment_settings(mock.url, {}, None, None, None)["verify"] == certifi.where()


def test_an_untrusted_certificate_names_ca_bundle_as_the_fix(https_folio):
    mock, _ca = https_folio
    with pytest.raises(FolioUnreachable) as err:
        _client(mock.url, tls_verify({"verify_tls": True}, None)).connect()
    text = str(err.value)
    assert "not trusted" in text and "folio.ca_bundle" in text and "windows" in text
    assert FOLIO_TEST_PASSWORD not in text


def test_tls_verify_settings():
    assert tls_verify({"verify_tls": True, "ca_bundle": None}) is True
    assert tls_verify({"verify_tls": False, "ca_bundle": "x.pem"}) is False
    with pytest.raises(ConfigError, match="ca_bundle"):
        tls_verify({"verify_tls": True, "ca_bundle": "missing.pem"}, "/nowhere")


def test_windows_store_is_refused_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(ConfigError, match="only works on Windows"):
        tls_verify({"verify_tls": True, "ca_bundle": "windows"})


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows certificate store")
def test_windows_store_checks_folio_with_the_system_certificates():
    ctx = tls_verify({"verify_tls": True, "ca_bundle": "Windows"})
    assert isinstance(ctx, ssl.SSLContext)
    client = _client("https://folio.example.org", ctx)
    adapter = client.session.get_adapter("https://folio.example.org/")
    assert isinstance(adapter, _ContextAdapter)
    assert adapter.poolmanager.connection_pool_kw["ssl_context"] is ctx
    assert client.session.verify is True


def test_runner_and_site_load_accept_a_ca_bundle(tmp_path, folio, monkeypatch):
    monkeypatch.setenv("FOLIO_PASSWORD", FOLIO_TEST_PASSWORD)
    root = make_site(tmp_path / "site", folio.url, folio={"ca_bundle": "campus.pem"})
    (root / "campus.pem").write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    site = load_site(root)
    assert tls_verify(site.folio, site.root) == str((root / "campus.pem").resolve())


def test_the_wizard_warns_when_certificates_are_not_checked(tmp_path, folio, monkeypatch):
    monkeypatch.setenv("FOLIO_PASSWORD", FOLIO_TEST_PASSWORD)
    root = make_site(tmp_path / "site", folio.url, folio={"verify_tls": False})
    p = ScriptedPrompter([False])                         # keep the connection; stop at the next question
    with pytest.raises(AssertionError, match="No scripted answer left"):
        run_wizard(root, p)
    text = "\n".join(p.transcript)
    assert "WARNING  folio.verify_tls is false" in text and "folio.ca_bundle" in text
    assert "Logged in" in text


# ---------------------------------------------------------------- SEC-6: no staff data in the cache


def test_items_are_cached_without_circulation_notes_or_staff_ids(tmp_path, monkeypatch):
    data = default_data()
    item = next(i for i in data["items"] if i["id"] == U(21))
    item["circulationNotes"] = [{"noteType": "Check in", "note": "Spilled coffee", "staffOnly": True,
                                 "source": {"id": U(950), "personal": {"lastName": "Staffer", "firstName": "Sam"}}}]
    item["lastCheckIn"] = {"dateTime": "2025-02-01T15:00:00.000+00:00", "servicePointId": U(960),
                           "staffMemberId": U(951)}
    item["notes"] = [{"itemNoteTypeId": U(970), "note": "Gift of the class of 1990", "staffOnly": True}]
    for rec in (item, data["holdingsRecords"][0], data["instances"][0]):
        rec["metadata"].update(createdByUserId=U(952), updatedByUserId=U(953), updatedByUsername="sstaffer")
    with MockFolio(data) as mock:
        monkeypatch.setenv("FOLIO_PASSWORD", FOLIO_TEST_PASSWORD)
        site = load_site(make_site(tmp_path / "site", mock.url))
        client = FolioClient(mock.url, "diku", "fx_reader", FOLIO_TEST_PASSWORD)
        cache = Cache(site.cache_path)
        try:
            Syncer(client, cache, site).run({"instances", "holdings", "items"})
            rows = {table: {i: unpack(d) for i, d in cache.db.execute(f"SELECT id, data FROM {table}")}
                    for table in ("items", "holdings", "instances")}
        finally:
            cache.close()
    cached = rows["items"][U(21)]
    assert "circulationNotes" not in cached
    assert cached["lastCheckIn"] == {"dateTime": "2025-02-01T15:00:00.000+00:00"}
    assert cached["notes"] == item["notes"]                                  # item notes are kept
    for rec in (cached, rows["holdings"][data["holdingsRecords"][0]["id"]],
                rows["instances"][data["instances"][0]["id"]]):
        assert rec["metadata"]["createdDate"]
        assert not {"createdByUserId", "updatedByUserId", "updatedByUsername"} & set(rec["metadata"])
    blob = repr(rows)
    assert U(951) not in blob and U(952) not in blob and "Spilled coffee" not in blob and "Staffer" not in blob


def test_item_notes_survive_the_strip():
    from folio_exchange.sync import strip_staff_data

    rec = {"id": "x", "notes": [{"itemNoteTypeId": "t", "note": "42", "staffOnly": True}],
           "lastCheckIn": {"servicePointId": "sp"}, "metadata": {"createdDate": "2024-01-01T00:00:00Z"}}
    out = strip_staff_data(rec)
    assert out["notes"] == [{"itemNoteTypeId": "t", "note": "42", "staffOnly": True}]
    assert "lastCheckIn" not in out and out["metadata"] == {"createdDate": "2024-01-01T00:00:00Z"}


# ---------------------------------------------------------------- SEC-3: who can change the folders


class _Site:
    def __init__(self, root):
        self.root = root
        self.feeds_dir = root / "feeds"
        self.recipe_dirs = [root / "recipes"]


def _private_site(tmp_path):
    root = tmp_path / "site"
    (root / "feeds").mkdir(parents=True)
    (root / "site.yaml").write_text("folio: {}\n", encoding="utf-8")
    return root


@pytest.mark.skipif(sys.platform == "win32", reason="mode bits")
def test_permissions_flag_a_world_writable_site_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(permissions, "install_paths", lambda: [])
    root = _private_site(tmp_path)
    for p in (root, root / "feeds"):
        os.chmod(p, 0o700)
    os.chmod(root / "site.yaml", 0o600)
    assert permissions.problems(_Site(root)) == []
    os.chmod(root / "feeds", 0o777)
    found = permissions.problems(_Site(root))
    assert len(found) == 1 and str(root / "feeds") in found[0] and "everyone" in found[0]
    assert "chmod -R go-w" in found[0]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows access lists")
def test_permissions_flag_a_folder_that_windows_users_can_modify(tmp_path, monkeypatch):
    monkeypatch.setattr(permissions, "install_paths", lambda: [])
    root = _private_site(tmp_path)
    # A folder of our own, cut off from what it inherits, so the result does not depend on the temp folder.
    me = f"{os.environ['USERDOMAIN']}\\{os.environ['USERNAME']}"
    subprocess.run(["icacls", str(root), "/inheritance:r", "/grant:r", f"{me}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F",
                    "*S-1-5-32-544:(OI)(CI)F"], check=True, capture_output=True)
    assert permissions.problems(_Site(root)) == []
    subprocess.run(["icacls", str(root), "/grant", "*S-1-5-32-545:(OI)(CI)M"], check=True, capture_output=True)
    found = permissions.problems(_Site(root))
    assert len(found) == 1, found                 # site.yaml and feeds inherit it: one fix, one message
    assert str(root) in found[0] and "Other Windows users can change the site folder" in found[0]
    assert f'icacls "{root}" /inheritance:r' in found[0]


def test_permissions_parse_icacls_output():
    path = "C:\\FOLIO-Exchange"
    text = (f"{path} BUILTIN\\Administrators:(I)(OI)(CI)(F)\r\n"
            "                 NT AUTHORITY\\SYSTEM:(I)(OI)(CI)(F)\r\n"
            "                 BUILTIN\\Users:(I)(OI)(CI)(RX)\r\n"
            "                 NT AUTHORITY\\Authenticated Users:(I)(OI)(CI)(IO)(M)\r\n"
            "                 Everyone:(DENY)(W)\r\n"
            "                 CREATOR OWNER:(I)(OI)(CI)(IO)(F)\r\n\r\n"
            "Successfully processed 1 files; Failed processing 0 files\r\n")
    entries = {e["who"]: e for e in permissions.parse_icacls(text, path)}
    assert set(entries) == {"BUILTIN\\Administrators", "NT AUTHORITY\\SYSTEM", "BUILTIN\\Users",
                            "NT AUTHORITY\\Authenticated Users", "Everyone", "CREATOR OWNER"}
    assert "M" in entries["NT AUTHORITY\\Authenticated Users"]["rights"]
    assert entries["Everyone"]["deny"] and entries["BUILTIN\\Users"]["inherited"]


def test_permissions_never_raise(monkeypatch):
    monkeypatch.setattr(permissions, "install_paths", lambda: 1 / 0)
    assert permissions.problems(object()) == [
        "Could not check who can change the FOLIO-Exchange folders (AttributeError)."]


def test_permissions_say_when_icacls_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(permissions, "install_paths", lambda: [])
    monkeypatch.setattr(permissions.sys, "platform", "win32")
    monkeypatch.setattr(permissions, "_current_sid", lambda: None)       # the Windows API cannot be used
    monkeypatch.setattr(permissions, "_icacls", lambda path: None)
    monkeypatch.setattr(permissions, "_trusted_names", lambda: set())
    found = permissions.problems(_Site(_private_site(tmp_path)))
    assert found == ["Could not check who can change the FOLIO-Exchange folders: the Windows icacls command is "
                     "not available."]


def test_permissions_say_when_icacls_times_out(tmp_path, monkeypatch):
    monkeypatch.setattr(permissions, "install_paths", lambda: [])
    monkeypatch.setattr(permissions.sys, "platform", "win32")
    monkeypatch.setattr(permissions, "_current_sid", lambda: None)

    def slow(path):
        raise subprocess.TimeoutExpired(["icacls", str(path)], permissions.TIMEOUT)

    monkeypatch.setattr(permissions, "_icacls", slow)
    found = permissions.problems(_Site(_private_site(tmp_path)))
    assert len(found) == 1 and "did not answer within 10 seconds" in found[0] and "not available" not in found[0]


ME = "S-1-5-21-1111111111-2222222222-3333333333-1001"
OTHER = "S-1-5-21-1111111111-2222222222-3333333333-1002"


def _by_sid(monkeypatch, entries_for):
    """Windows, with each path's access list given by ``entries_for(path)`` instead of read from the disk."""
    monkeypatch.setattr(permissions.sys, "platform", "win32")
    monkeypatch.setattr(permissions, "_current_sid", lambda: ME)
    monkeypatch.setattr(permissions, "_acl_entries", entries_for)
    monkeypatch.setattr(permissions, "_name_of_sid", lambda sid: {OTHER: "PC\\Jo"}.get(sid))
    monkeypatch.setattr(permissions, "_icacls", lambda path: pytest.fail("icacls is not needed"))


def _ace(sid, write=True, inherited=False, deny=False):
    return {"who": sid, "sid": sid, "write": write, "inherited": inherited, "deny": deny}


def test_permissions_match_accounts_by_sid(tmp_path, monkeypatch):
    monkeypatch.setattr(permissions, "install_paths", lambda: [])
    root = _private_site(tmp_path).resolve()
    acl = [_ace("S-1-5-18"), _ace("S-1-5-32-544"), _ace(ME), _ace("S-1-3-0"),
           _ace("S-1-15-2-1"), _ace("S-1-15-3-1024-1065365936-1281604716"),     # app packages, not people
           _ace("S-1-5-32-545", write=False), _ace("S-1-1-0", deny=True)]
    _by_sid(monkeypatch, lambda path: acl)
    assert permissions.problems(_Site(root)) == []
    _by_sid(monkeypatch, lambda path: acl + [_ace(OTHER)] if path == root else acl)
    found = permissions.problems(_Site(root))
    assert len(found) == 1 and "the site folder" in found[0] and "(PC\\Jo)" in found[0]
    assert f'"*{ME}:(OI)(CI)F"' in found[0]                     # the fix names this user by SID too
    _by_sid(monkeypatch, lambda path: acl + [_ace("S-1-5-21-9-9-9-1500")])     # a name that cannot be looked up
    assert "(S-1-5-21-9-9-9-1500)" in permissions.problems(_Site(root))[0]


def test_permissions_fall_back_to_names_and_read_question_marks_safely(tmp_path, monkeypatch):
    monkeypatch.setattr(permissions, "install_paths", lambda: [])
    monkeypatch.setattr(permissions.sys, "platform", "win32")
    monkeypatch.setattr(permissions, "_current_sid", lambda: None)
    monkeypatch.setattr(permissions, "_trusted_names", lambda: {"nt authority\\system", "pc\\zoë"})
    root = _private_site(tmp_path).resolve()
    listing = {"ok": "NT AUTHORITY\\SYSTEM:(OI)(CI)(F)\r\n PC\\Zo?:(OI)(CI)(F)\r\n"
                     " APPLICATION PACKAGE AUTHORITY\\ALL APPLICATION PACKAGES:(OI)(CI)(M)\r\n"}
    monkeypatch.setattr(permissions, "_icacls", lambda path: f"{path} " + listing["ok"])
    assert permissions.problems(_Site(root)) == []                # "Zo?" is this user, printed in the code page
    others = " PC\\Al?:(OI)(CI)(M)\r\n ??:(OI)(CI)(M)\r\n"
    monkeypatch.setattr(permissions, "_icacls", lambda path: f"{path} " + listing["ok"] + (others if path == root
                                                                                            else ""))
    found = permissions.problems(_Site(root))
    assert len(found) == 1 and "(??, PC\\Al?)" in found[0]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows access lists")
def test_permissions_read_real_access_lists_quickly(tmp_path, monkeypatch):
    import time

    root = _private_site(tmp_path)
    me = f"{os.environ['USERDOMAIN']}\\{os.environ['USERNAME']}"
    subprocess.run(["icacls", str(root), "/inheritance:r", "/grant:r", f"{me}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F",
                    "*S-1-15-2-1:(OI)(CI)M"], check=True, capture_output=True)
    assert permissions._acl_entries(root) is not None
    monkeypatch.setattr(permissions, "_icacls", lambda path: pytest.fail("icacls is not needed"))
    started = time.monotonic()
    found = permissions.problems(_Site(root))                    # with the real program and Python folders
    assert time.monotonic() - started < 0.5
    assert not [w for w in found if str(root) in w]              # this user and an app package: nobody else


def test_install_paths_cover_the_environment_and_the_python_under_it(tmp_path, monkeypatch):
    venv, base = tmp_path.resolve() / "venv", tmp_path.resolve() / "Python312"
    for d in (venv / "Scripts", base / "Lib"):
        d.mkdir(parents=True)
    monkeypatch.setattr(permissions.sys, "prefix", str(venv))
    monkeypatch.setattr(permissions.sys, "base_prefix", str(base))
    paths = permissions.install_paths()
    assert venv in paths and base in paths and base / "Lib" in paths
    assert paths.index(venv) < paths.index(base)
    assert permissions._install_label(venv) == "the installed FOLIO-Exchange program"
    assert permissions._install_label(base) == permissions._install_label(base / "Lib") == (
        "the Python that FOLIO-Exchange runs on")
    _by_sid(monkeypatch, lambda path: [_ace(ME)] + ([_ace(OTHER, inherited=True)] if base in (path, *path.parents)
                                                   else []))
    root = _private_site(tmp_path)
    found = permissions.problems(_Site(root))
    assert len(found) == 1, found                                # Lib inherits it: one fix, one message
    assert f"the Python that FOLIO-Exchange runs on, {base} (PC\\Jo)" in found[0]


def test_the_wizard_suggests_a_folder_of_ones_own_for_a_synced_one(tmp_path):
    p = ScriptedPrompter([False])
    assert run_wizard(tmp_path / "OneDrive" / "site", p) == 1
    text = "\n".join(p.transcript)
    assert "%LOCALAPPDATA%\\FOLIO-Exchange\\<tenant>" in text and "C:\\FOLIO-Exchange" not in text


def test_the_windows_store_wording_also_names_ca_bundle(monkeypatch):
    import requests

    client = _client("https://folio.example.org", True)

    def refuse(*a, **kw):
        raise requests.exceptions.SSLError("Max retries exceeded (Caused by SSLError(SSLCertVerificationError("
                                           "'A certificate chain processed, but terminated in a root certificate "
                                           "which is not trusted by the trust provider.')))")

    monkeypatch.setattr(client.session, "post", refuse)
    with pytest.raises(FolioUnreachable, match="folio.ca_bundle"):
        client.connect()


def test_check_and_the_page_show_folder_access_and_recipe_copy_warnings(site, monkeypatch, capsys):
    from datetime import datetime, timezone

    from folio_exchange import feedops, permissions
    from folio_exchange.cli import main
    from folio_exchange.config import BUNDLED_RECIPES, load_site
    from folio_exchange.history import History
    from folio_exchange.recipe import RecipeLibrary, load_all

    monkeypatch.setattr(permissions, "problems", lambda s: ["Other Windows users can change the site folder."])
    copy = site / "recipes" / "chq-academic"
    copy.mkdir(parents=True)
    text = (BUNDLED_RECIPES / "chq-academic" / "1.0.0.yaml").read_text(encoding="utf-8")
    (copy / "1.0.0.yaml").write_text(text.replace("title: collectionHQ Academic", "title: Our copy", 1),
                                     encoding="utf-8")
    s = load_site(site)
    library = RecipeLibrary(s.recipe_dirs)
    feeds, errors = load_all(s, library)
    with History(s.history_path) as hist:
        codes = {w["code"] for w in feedops.site_warnings(s, library, feeds, errors, hist, datetime.now(timezone.utc))}
    assert {"folder-access", "recipe-shadow"} <= codes
    main(["-s", str(site), "recipes", "list"])
    assert "WARNING" in capsys.readouterr().out
