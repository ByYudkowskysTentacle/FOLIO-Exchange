import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))


@pytest.fixture(autouse=True)
def no_real_keyring(monkeypatch):
    """Never touch the developer's real credential store; tests that need one install a fake."""
    monkeypatch.setitem(sys.modules, "keyring", None)


@pytest.fixture(autouse=True)
def no_real_secrets_in_env(monkeypatch):
    """Tests decide which secrets exist; a developer's own environment must not leak in."""
    for name in ("FOLIO_PASSWORD", "FOLIO_URL", "FOLIO_TENANT", "FOLIO_USERNAME", "NOTIFY_WEBHOOK_URL",
                 "FOLIO_EXCHANGE_SITE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def folio():
    """A running mock FOLIO with the default fixture data; loan dates marked RECENT are made relative to now."""
    from mock_folio import MockFolio, default_data
    from sitekit import recent

    data = default_data()
    for loan in data["loans"]:
        if loan.get("loanDate") == "RECENT":
            loan["loanDate"] = recent(30)
        if loan.get("returnDate") == "RECENT_RETURN":
            loan["returnDate"] = recent(10)
    with MockFolio(data) as m:
        yield m


@pytest.fixture
def site(tmp_path, folio, monkeypatch):
    """A site folder wired to the mock FOLIO, with one feed `chq` on chq-academic delivering to a local folder."""
    from sitekit import FOLIO_TEST_PASSWORD, make_feed, make_site

    monkeypatch.setenv("FOLIO_PASSWORD", FOLIO_TEST_PASSWORD)
    root = make_site(tmp_path / "site", folio.url)
    make_feed(root)
    return root
