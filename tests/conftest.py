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


@pytest.fixture(autouse=True)
def no_real_app_settings(monkeypatch, tmp_path_factory):
    """The desktop app's per-user settings (its recent sites) go to a test folder, never the developer's own."""
    from folio_exchange.desktop import places

    folder = tmp_path_factory.mktemp("app-settings")
    monkeypatch.setattr(places, "config_dir", lambda: folder)


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
