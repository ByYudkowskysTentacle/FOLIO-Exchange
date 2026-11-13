import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")     # before any QApplication: widget tests show nothing


def _qt_problem() -> str | None:
    """Why the desktop app's widget tests cannot run here (PySide6 or pytest-qt missing, or a Qt that fails to
    load, as with an old VC++ runtime), or None when they can."""
    try:
        import PySide6.QtNetwork  # noqa: F401
        import PySide6.QtWidgets  # noqa: F401
        import pytestqt  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - any failure to load means the same: no widget tests here
        return f"{type(exc).__name__}: {exc}"
    return None


def require_qt_if_asked(problem: str | None, environ=os.environ) -> None:
    """CI sets FX_REQUIRE_QT=1: there a Qt that does not load (a Linux library missing, say) stops the run with
    the reason, rather than quietly skip every widget test and pass."""
    if problem and environ.get("FX_REQUIRE_QT"):
        pytest.exit(f"FX_REQUIRE_QT is set, but the desktop app's tests cannot run: {problem}", returncode=1)


# pytest-qt is turned off in pyproject.toml (it stops the whole run when Qt is missing) and loaded here instead.
QT_PROBLEM = _qt_problem()
QT_LOADS = QT_PROBLEM is None
require_qt_if_asked(QT_PROBLEM)
pytest_plugins = ["pytestqt.plugin"] if QT_LOADS else []


@pytest.fixture(autouse=True)
def qt_garbage_on_the_main_thread(request):
    """A Qt test collects cyclic garbage on the main thread only, as the app does (worker.MainThreadCollector).
    Reads and jobs run in threads of their own, and a collection there that frees a Qt object only a cycle kept
    alive (a test's window holding a test's function that holds the window, say) crashes the whole run. Before
    automatic collection is back on, the widgets the test closed are deleted and its cycles freed, here."""
    if not QT_LOADS or not {"qtbot", "qapp"} & set(request.fixturenames):
        yield
        return
    from PySide6.QtCore import QCoreApplication, QEvent

    from folio_exchange.desktop.worker import MainThreadCollector

    request.getfixturevalue("qapp")
    collector = MainThreadCollector().start()
    yield
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)     # the windows qtbot closed
    collector.stop()


# Never touch the developer's real credential store, not even from a thread that outlives its test (a window's
# reading of the site, or a look at FOLIO still retrying after the mock FOLIO stopped): "keyring" cannot be imported
# for the whole run. Regression: blocked only during each test, a leftover thread imported the real package between
# tests, and the next test's secrets went to Windows Credential Manager instead of its fake.
sys.modules["keyring"] = None


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


def _real_app_folders():
    """The app's real folder functions, kept before every test redirects them: a test may compute them, never
    write there."""
    from folio_exchange.desktop import places

    return places.config_dir, places.local_dir


REAL_APP_FOLDERS = _real_app_folders()


@pytest.fixture(autouse=True)
def no_real_app_settings(monkeypatch, tmp_path_factory):
    """The desktop app's per-user settings (its recent sites, the tray's list) go to test folders, never the
    developer's own."""
    from folio_exchange.desktop import places

    folder = tmp_path_factory.mktemp("app-settings")
    local = tmp_path_factory.mktemp("app-local")
    monkeypatch.setattr(places, "config_dir", lambda: folder)
    monkeypatch.setattr(places, "local_dir", lambda: local)


class FakeRegistry:
    """Stands in for winreg: the tray's Startup entry (desktop/startup.py) goes here, never to the developer's
    own Run key. ``values`` maps (key path, value name) to the text stored."""
    HKEY_CURRENT_USER, KEY_READ, KEY_SET_VALUE, REG_SZ = "HKCU", 0x20019, 0x0002, 1

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.refuse: OSError | None = None              # set to make every write fail, as a locked-down PC might

    class Key:
        def __init__(self, path: str) -> None:
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def OpenKey(self, _root, path, _reserved=0, _access=0):  # noqa: N802 - winreg's names
        return self.Key(path)

    def CreateKeyEx(self, _root, path, _reserved=0, _access=0):  # noqa: N802
        if self.refuse:
            raise self.refuse
        return self.Key(path)

    def QueryValueEx(self, key, name):  # noqa: N802
        if (key.path, name) not in self.values:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        return self.values[(key.path, name)], self.REG_SZ

    def SetValueEx(self, key, name, _reserved, _kind, value):  # noqa: N802
        if self.refuse:
            raise self.refuse
        self.values[(key.path, name)] = value

    def DeleteValue(self, key, name):  # noqa: N802
        if self.refuse:
            raise self.refuse
        if self.values.pop((key.path, name), None) is None:
            raise FileNotFoundError(2, "The system cannot find the file specified")


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    """Never read or write the developer's real Windows registry; on every system the tests see a Windows-like
    one (tests that need "not Windows" replace startup._winreg themselves)."""
    from folio_exchange.desktop import startup

    fake = FakeRegistry()
    monkeypatch.setattr(startup, "_winreg", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def no_real_desktop(monkeypatch, tmp_path_factory):
    """A site renamed in a test carries its desktop launcher along (siteops.rename_site): that is a test folder,
    never the developer's own Desktop, where it could rewrite or delete a real launcher."""
    from folio_exchange import launcher

    folder = tmp_path_factory.mktemp("desktop")
    monkeypatch.setattr(launcher, "desktop", lambda: folder)


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
