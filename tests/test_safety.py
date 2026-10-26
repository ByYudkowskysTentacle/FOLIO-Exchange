"""Patron data, read-only access, redirects and secrets."""
import pytest
from sitekit import make_site

from folio_exchange import client as client_mod
from folio_exchange.client import FolioClient, FolioError, guard
from folio_exchange.config import load_site, reject_secrets, write_yaml
from folio_exchange.errors import ConfigError, PatronDataRefused
from folio_exchange.reads import READS, is_allowed_path, is_patron_path, permissions_for


@pytest.mark.parametrize("path", [
    "/users", "/users/123", "/USERS?query=x", "/bl-users/by-id/1", "/circulation/loans", "/circulation/requests",
    "/loan-storage/loan-history", "/request-storage/requests", "/accounts", "/notes", "/perms/users",
    "/patron-pin/verify", "/users-keycloak/users", "/data-export-spring/jobs",
])
def test_patron_paths_are_refused_before_sending(path):
    assert is_patron_path(path)
    with pytest.raises(PatronDataRefused):
        guard("GET", path)


@pytest.mark.parametrize("path", ["/user", "/usersettings", "/loan-storage/loans", "/notesx"])
def test_patron_denylist_matches_whole_segments(path):
    assert not is_patron_path(path)


def test_only_registry_paths_and_only_reads():
    assert is_allowed_path("/item-storage/items?query=x")
    assert not is_allowed_path("/item-storage/items/123")
    with pytest.raises(FolioError, match="not a path in the reads registry"):
        guard("GET", "/inventory/items")
    for method, path in (("POST", "/item-storage/items"), ("PUT", "/item-storage/items"),
                         ("DELETE", "/locations")):
        with pytest.raises(FolioError, match="read-only"):
            guard(method, path)
    guard("POST", READS["marc"].path)                                # a read by POST
    guard("POST", "/authn/login-with-expiry")


def test_client_refuses_patron_paths_without_any_request(folio):
    c = FolioClient(folio.url, "diku", "u", "secret")
    folio.requests.clear()
    with pytest.raises(PatronDataRefused):
        c.get("/users", {"limit": 1})
    assert folio.requests == []


def test_redirects_are_never_followed(monkeypatch):
    from unittest import mock

    c = FolioClient("https://folio.example", "t", "u", "p")
    c._token, c._expires = "tok", None
    resp = mock.Mock(status_code=302, ok=False, headers={"Location": "https://elsewhere.example/x"})
    calls = []
    monkeypatch.setattr(c.session, "request", lambda *a, **k: calls.append(k) or resp)
    with pytest.raises(FolioError, match="redirect"):
        c.get("/locations")
    assert calls[0]["allow_redirects"] is False
    assert c.session.max_redirects == 0


def test_permissions_cover_reads_and_reference_data():
    perms = permissions_for(["instances", "loans"])
    assert "inventory-storage.instances.collection.get" in perms
    assert "circulation-storage.loans.collection.get" in perms
    assert "inventory-storage.locations.collection.get" in perms
    assert not any("users" in p for p in perms)


def test_secret_keys_are_refused_anywhere_in_a_file(tmp_path):
    with pytest.raises(ConfigError, match=r"delivery\.password: secrets never go in files"):
        reject_secrets({"delivery": {"password": ""}}, "f.yaml")
    with pytest.raises(ConfigError, match=r"alerts\.webhook_url"):
        reject_secrets({"alerts": {"webhook_url": "x"}}, "f.yaml")
    with pytest.raises(ConfigError):
        write_yaml(tmp_path / "x.yaml", {"folio": {"token": "abc"}})
    assert not (tmp_path / "x.yaml").exists()


def test_site_requires_https_except_on_this_computer(tmp_path):
    make_site(tmp_path / "a", "http://folio.example.edu")
    with pytest.raises(ConfigError, match="https://"):
        load_site(tmp_path / "a")
    make_site(tmp_path / "b", "http://127.0.0.1:9130")
    assert load_site(tmp_path / "b").folio["base_url"].startswith("http://127.0.0.1")


def test_smtp_login_needs_tls(tmp_path, folio):
    make_site(tmp_path, folio.url, alerts={"email": {"to": "a@b.org", "from": "c@d.org", "smtp_host": "smtp.x",
                                                     "starttls": False, "username": "me"}})
    with pytest.raises(ConfigError, match="starttls"):
        load_site(tmp_path)


def test_errors_never_include_the_password(folio, monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)
    c = FolioClient(folio.url, "diku", "u", "wrong-pass-123")
    with pytest.raises(FolioError) as err:
        c.login()
    assert "wrong-pass-123" not in str(err.value)
