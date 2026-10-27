"""Renaming the site folder from the page (Options) and the command line (`rename-site`)."""
import logging
import types

import pytest
from sitekit import make_feed
from test_setup import keyring  # noqa: F401 - the in-memory keyring fixture
from test_ui import call

from folio_exchange import credentials, launcher, siteops, ui
from folio_exchange.cli import main
from folio_exchange.config import load_site
from folio_exchange.errors import ConfigError, FxError
from folio_exchange.history import History
from folio_exchange.lock import RunLock


@pytest.fixture
def lib_site(site):
    """The test site, with its local delivery folder outside it (a full path into the folder blocks a rename)."""
    make_feed(site, delivery={"transport": "local", "directory": str(site.parent / "vendor-drop")})
    return site


def no_task(*_a, **_k):
    return types.SimpleNamespace(returncode=1)


@pytest.mark.parametrize("name, why", [
    ("", "Enter a name"), ("a/b", "cannot contain"), ("a:b", "cannot contain"), ("lib.", "end with a full stop"),
    (" lib", "start or end with a space"), ("CON", "Windows keeps"), ("nul.txt", "Windows keeps"),
    ("A & B", "Command Prompt"), ("100%", "Command Prompt"), ("x" * 81, "80 characters"),
])
def test_names_that_would_break_on_windows_are_refused(name, why):
    assert why in siteops.name_problem(name)


def test_ordinary_library_names_are_fine():
    for name in ("Lakeside College Library", "lakeside-library", "St. Mary's (main)", "Bibliothèque 2"):
        assert siteops.name_problem(name) is None


def test_the_folder_is_renamed_and_the_site_carries_on(lib_site, monkeypatch, tmp_path):
    monkeypatch.setattr(siteops.platform, "system", lambda: "Linux")
    res = siteops.rename_site(lib_site, "Lakeside Library", via="cli", desktop=tmp_path / "no-desktop", run=no_task)
    new = lib_site.parent / "Lakeside Library"
    assert res["root"] == str(new.resolve()) and res["name"] == "Lakeside Library" and res["task"] is None
    assert not lib_site.exists() and (new / "site.yaml").is_file() and (new / "feeds" / "chq.yaml").is_file()
    site = load_site(new)
    assert site.name == "Lakeside Library"
    with History(site.history_path) as hist:
        entry = hist.audit_log(5)[0]
    assert entry["action"] == "site folder renamed" and entry["detail"]["to"] == str(new.resolve())
    assert main(["-s", str(new), "run", "chq"]) == 2       # held (the first delivery): the site works from here


def test_a_run_in_progress_or_a_taken_name_changes_nothing(lib_site, tmp_path):
    (lib_site.parent / "taken").mkdir()
    with pytest.raises(ConfigError, match="already exists"):
        siteops.rename_site(lib_site, "taken", via="cli", run=no_task)
    with pytest.raises(ConfigError, match="already called"):
        siteops.rename_site(lib_site, lib_site.name, via="cli", run=no_task)
    with RunLock(load_site(lib_site).lock_path):
        with pytest.raises(FxError, match="Another FOLIO-Exchange run"):
            siteops.rename_site(lib_site, "other", via="cli", run=no_task)
    assert lib_site.is_dir() and not (lib_site.parent / "other").exists()


def test_a_full_path_into_the_folder_is_named_and_nothing_is_changed(site):
    # the test feed delivers to <site>/vendor-drop by its full path
    with pytest.raises(ConfigError, match=r"feeds/chq.yaml: delivery.directory = .*vendor-drop.*Nothing was changed"):
        siteops.rename_site(site, "other", via="cli", run=no_task)
    assert site.is_dir()


def test_a_folder_windows_will_not_rename_is_explained(lib_site, monkeypatch):
    def refuse(_a, _b):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(siteops.os, "rename", refuse)
    with pytest.raises(FxError, match="a file in it is open.*Nothing was changed"):
        siteops.rename_site(lib_site, "other", via="cli", run=no_task)
    assert lib_site.is_dir()


def test_this_process_log_file_follows_the_folder(lib_site, monkeypatch, tmp_path):
    monkeypatch.setattr(siteops.platform, "system", lambda: "Linux")
    (lib_site / "logs").mkdir(exist_ok=True)
    handler = logging.FileHandler(lib_site / "logs" / "folio-exchange.log", encoding="utf-8")
    logging.getLogger().addHandler(handler)
    try:
        logging.getLogger("folio_exchange.test").warning("before the rename")
        siteops.rename_site(lib_site, "renamed", via="ui", desktop=tmp_path, run=no_task)
        logging.getLogger("folio_exchange.test").warning("after the rename")
        handler.flush()
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
    text = (lib_site.parent / "renamed" / "logs" / "folio-exchange.log").read_text(encoding="utf-8")
    assert "before the rename" in text and "after the rename" in text


def test_alert_addresses_and_the_launcher_come_along(lib_site, keyring, monkeypatch, tmp_path):  # noqa: F811
    from sitekit import site_data, write_yaml

    monkeypatch.setattr(siteops.platform, "system", lambda: "Windows")
    data = site_data(load_site(lib_site).folio["base_url"])
    data["alerts"] = {"on": "failure", "webhook": "keyring", "heartbeat": "keyring"}
    write_yaml(lib_site / "site.yaml", data)
    keyring[(credentials.WEBHOOK_SERVICE, lib_site.name)] = "https://hooks.example/abc"
    desk = tmp_path / "Desktop"
    desk.mkdir()
    old_launcher = launcher.write(lib_site, desk, "fx.exe", "Windows")
    own = desk / "FOLIO-Exchange (notes).cmd"
    own.write_bytes(b"@echo mine\r\n")
    res = siteops.rename_site(lib_site, "Lakeside", via="ui", desktop=desk, run=no_task)
    assert keyring[(credentials.WEBHOOK_SERVICE, "Lakeside")] == "https://hooks.example/abc"
    assert keyring[(credentials.WEBHOOK_SERVICE, lib_site.name)] == "https://hooks.example/abc"   # kept
    assert (credentials.HEARTBEAT_SERVICE, "Lakeside") not in keyring        # none was stored: nothing made up
    assert not old_launcher.exists() and own.read_bytes() == b"@echo mine\r\n"
    new_launcher = desk / "FOLIO-Exchange (Lakeside).cmd"
    assert str(lib_site.parent / "Lakeside") in new_launcher.read_text(encoding="utf-8")
    assert any("alert webhook address" in n for n in res["notes"])
    assert any("launcher on the desktop" in n for n in res["notes"])


def test_the_scheduled_task_is_named_with_the_commands_to_move_it(lib_site, monkeypatch, tmp_path):
    monkeypatch.setattr(siteops.platform, "system", lambda: "Windows")
    asked = []

    def schtasks(args, **_k):
        asked.append(args)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setenv("FOLIO_EXCHANGE_SITE", str(lib_site.resolve()))
    res = siteops.rename_site(lib_site, "Lakeside", via="ui", desktop=tmp_path, run=schtasks)
    assert asked == [["schtasks", "/Query", "/TN", f"FOLIO-Exchange ({lib_site.name})"]]
    task = res["task"]
    assert task["old"] == f"FOLIO-Exchange ({lib_site.name})" and task["new"] == "FOLIO-Exchange (Lakeside)"
    assert "schedule --register" in task["register"] and "Lakeside" in task["register"]
    assert task["remove"] == f'schtasks /Delete /TN "FOLIO-Exchange ({lib_site.name})" /F'
    assert any("Windows password" in n for n in res["notes"])
    assert any("setx FOLIO_EXCHANGE_SITE" in n for n in res["notes"])


def test_the_command_line_renames_registers_and_removes_the_old_task(lib_site, monkeypatch, capsys):
    import folio_exchange.cli as cli

    monkeypatch.setattr(siteops.platform, "system", lambda: "Windows")
    monkeypatch.setattr(cli.platform, "system", lambda: "Windows")
    calls = []

    def schtasks(args, **_k):
        calls.append(list(args))
        return types.SimpleNamespace(returncode=0)

    answers = iter(["y", "y"])
    monkeypatch.setattr("builtins.input", lambda _p: next(answers))
    args = cli.build_parser().parse_args(["-s", str(lib_site), "rename-site", "Lakeside"])
    assert cli.cmd_rename_site(args, run=schtasks) == 0
    out = capsys.readouterr().out
    assert "Renamed." in out and "Removed the old task." in out
    assert calls[0][:2] == ["schtasks", "/Query"]
    assert calls[1][:3] == ["schtasks", "/Create", "/TN"] and "FOLIO-Exchange (Lakeside)" in calls[1]
    assert calls[2] == ["schtasks", "/Delete", "/TN", f"FOLIO-Exchange ({lib_site.name})", "/F"]


def test_the_command_line_asks_first(lib_site, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _p: "n")
    assert main(["-s", str(lib_site), "rename-site", "Lakeside"]) == 2
    assert lib_site.is_dir() and "Nothing was changed" in capsys.readouterr().out
    assert main(["-s", str(lib_site), "rename-site", "bad/name", "--yes"]) == 1


def test_the_page_renames_its_own_folder_and_keeps_serving_it(lib_site, monkeypatch, tmp_path):
    monkeypatch.setattr(siteops.platform, "system", lambda: "Linux")
    monkeypatch.setattr(launcher, "desktop", lambda: tmp_path / "no-desktop")
    httpd, app = ui.start_in_thread(lib_site)
    try:
        status, before, _ = call(app, "/api/site")
        assert status == 200 and before["name"] == lib_site.name and before["parent"] == str(lib_site.resolve().parent)
        status, err, _ = call(app, "/api/site/rename", {"name": "a|b"})
        assert status == 400 and "cannot contain" in err["error"]
        status, res, _ = call(app, "/api/site/rename", {"name": "Lakeside Library"})
        assert status == 200 and res["name"] == "Lakeside Library"
        status, after, _ = call(app, "/api/site")
        assert status == 200 and after["name"] == "Lakeside Library" and after["root"] == res["root"]
        assert [f["name"] for f in after["feeds"]] == ["chq"]
        assert call(app, "/api/audit")[1][0]["action"] == "site folder renamed"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_page_will_not_rename_while_an_action_runs(lib_site):
    httpd, app = ui.start_in_thread(lib_site)
    try:
        app.jobs.current = types.SimpleNamespace(status="running", kind="run", feed="chq")
        status, err, _ = call(app, "/api/site/rename", {"name": "other"})
        assert status == 400 and "Wait for the run of chq to finish" in err["error"]
    finally:
        app.jobs.current = None
        httpd.shutdown()
        httpd.server_close()
    assert lib_site.is_dir()


def test_the_page_has_an_options_view_for_the_folder():
    from pathlib import Path

    web = Path(ui.__file__).with_name("web")
    assert 'id="nav-options"' in (web / "index.html").read_text(encoding="utf-8")
    js = (web / "app.js").read_text(encoding="utf-8")
    assert "function renderOptions()" in js and '"/api/site/rename"' in js
