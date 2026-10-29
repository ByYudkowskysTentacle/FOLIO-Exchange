"""The desktop app's parts that need no Qt: recent sites, folder checks, the instance name, the words it shows,
and starting it when Qt is missing. These run with or without the desktop extra."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from folio_exchange import cli, desktop, siteops
from folio_exchange.desktop import model, places


def test_recent_sites_newest_first_at_most_ten(tmp_path):
    folders = []
    for i in range(12):
        (tmp_path / f"site{i}").mkdir()
        folders.append(tmp_path / f"site{i}")
        places.remember_site(folders[-1])
    got = places.recent_sites()
    assert got == [f.resolve() for f in reversed(folders)][:places.RECENT_MOST]
    folders[11].rmdir()                                    # a folder that has gone stays listed: opening it says so
    assert places.recent_sites()[0] == folders[11].resolve()
    assert "not a folder that exists" in places.folder_problem(places.recent_sites()[0])
    places.remember_site(folders[5])                       # opened again: back to the top, once
    got = places.recent_sites()
    assert got[0] == folders[5].resolve() and got.count(folders[5].resolve()) == 1
    saved = json.loads((places.config_dir() / places.RECENT_FILE).read_text(encoding="utf-8"))
    assert all(isinstance(p, str) for p in saved)          # folder paths only


def test_recent_sites_follow_a_rename_and_survive_a_damaged_file(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    places.remember_site(old)
    old.rename(new)
    places.remember_site(new, replaces=old.resolve())
    assert places.recent_sites() == [new.resolve()]
    (places.config_dir() / places.RECENT_FILE).write_text("{not json", encoding="utf-8")
    assert places.recent_sites() == []


def test_the_recent_list_never_looks_at_the_site_folders(tmp_path, monkeypatch):
    """Regression: listing recent sites ran is_dir() on each folder on the window's thread, so a list holding a
    share that had stopped answering kept the picker from appearing, or froze the window, for a minute or more."""
    share = Path("//192.0.2.1/library/site" if sys.platform == "win32" else "/mnt/gone/site")
    places.remember_site(tmp_path)
    settings = places.config_dir()
    (settings / places.RECENT_FILE).write_text(json.dumps([str(share), str(tmp_path)]), encoding="utf-8")

    def guard(name):
        real = getattr(Path, name)

        def look(self, *a, **k):
            if self == settings or settings in self.parents:          # the app's own settings folder is fine
                return real(self, *a, **k)
            raise AssertionError(f"the recent list touched {self}")

        return look

    with monkeypatch.context() as m:
        for name in ("is_dir", "exists", "resolve", "stat", "iterdir"):
            m.setattr(Path, name, guard(name))
        assert places.recent_sites() == [share, tmp_path]
        assert places.remember_site(tmp_path / "renamed", replaces=tmp_path) == [tmp_path / "renamed", share]
    assert places.recent_sites() == [tmp_path / "renamed", share]


def test_a_new_site_left_unfinished_can_be_opened_again(tmp_path):
    """Regression: a site made with "Set up a new site" and closed before its setup was done held the app's log
    folder, so the picker refused it as no site, and setting it up again under its name as a folder in use."""
    root = tmp_path / "Main"
    siteops.make_site_folder(root)                         # as the picker's new-site dialog does
    (root / "logs").mkdir()
    (root / "logs" / "folio-exchange.log").write_text("Desktop app opened\n", encoding="utf-8")
    (root / "data").mkdir()
    (root / "data" / ".page-secret").write_text("x", encoding="ascii")      # once the page was opened
    places.remember_site(root)
    assert places.recent_sites() == [root.resolve()]
    assert places.unfinished_site(root)
    assert places.folder_problem(root) is None             # from the recent list or "Open a site folder"
    assert places.new_site_problem(tmp_path, "Main") is None
    (root / "notes.txt").write_text("mine", encoding="utf-8")        # anything else: not the app's own leftovers
    assert not places.unfinished_site(root) and "There is no site.yaml" in places.folder_problem(root)
    assert "already holds other files" in places.new_site_problem(tmp_path, "Main")
    (tmp_path / "plain").mkdir()                           # an empty folder never opened is still not a site
    assert "There is no site.yaml" in places.folder_problem(tmp_path / "plain")
    (tmp_path / "half" / "feeds").mkdir(parents=True)      # a site whose site.yaml went missing stays an error
    (tmp_path / "half" / "logs").mkdir()
    (tmp_path / "half" / "logs" / "folio-exchange.log").write_text("x", encoding="utf-8")
    assert "There is no site.yaml" in places.folder_problem(tmp_path / "half")


def test_the_settings_folder_is_redirected_in_tests():
    assert "app-settings" in str(places.config_dir())        # conftest's folder, never the developer's own


def test_a_folder_without_site_yaml_is_not_a_site(tmp_path):
    assert "There is no site.yaml" in places.folder_problem(tmp_path)
    assert "not a folder that exists" in places.folder_problem(tmp_path / "missing")
    (tmp_path / "site.yaml").write_text("folio: {}\n", encoding="utf-8")
    assert places.folder_problem(tmp_path) is None


def test_a_new_site_needs_a_good_name_in_a_folder_that_exists(tmp_path):
    assert places.new_site_problem(tmp_path, "Main Library") is None
    assert places.new_site_problem(tmp_path, "a/b") == siteops.name_problem("a/b")
    assert "keeps" in places.new_site_problem(tmp_path, "con")
    assert "not a folder that exists" in places.new_site_problem(tmp_path / "missing", "x")
    assert places.new_site_problem("", "x") == "Choose the folder to put the new site in."
    (tmp_path / "taken").mkdir()
    (tmp_path / "taken" / "site.yaml").write_text("x: 1\n", encoding="utf-8")
    assert "already a site" in places.new_site_problem(tmp_path, "taken")
    (tmp_path / "busy").mkdir()
    (tmp_path / "busy" / "notes.txt").write_text("x", encoding="utf-8")
    assert "already holds other files" in places.new_site_problem(tmp_path, "busy")
    (tmp_path / "empty").mkdir()
    assert places.new_site_problem(tmp_path, "empty") is None


def test_new_site_folders_are_made_only_inside_a_folder_that_exists(tmp_path):
    assert siteops.new_site_root(None) is None
    assert siteops.new_site_root(tmp_path / "fresh") == (tmp_path / "fresh").resolve()
    siteops.make_site_folder(tmp_path / "fresh")
    assert (tmp_path / "fresh").is_dir()
    with pytest.raises(siteops.ConfigError, match="does not exist"):
        siteops.make_site_folder(tmp_path / "no" / "such")
    (tmp_path / "half" / "feeds").mkdir(parents=True)       # a site whose site.yaml went missing stays an error
    assert siteops.new_site_root(tmp_path / "half") is None


def test_the_instance_name_is_per_site_and_says_nothing_about_it(tmp_path):
    a, b = places.instance_name(tmp_path / "a"), places.instance_name(tmp_path / "b")
    assert a != b and a == places.instance_name(tmp_path / "a")
    assert tmp_path.name not in a and str(tmp_path) not in a
    assert len(a) < 60                                       # macOS socket paths are short


def test_words_for_people():
    assert model.password_words("system keyring (folio-exchange)") == "Stored in Windows Credential Manager (folio-exchange)"
    assert model.password_words("environment (FOLIO_PASSWORD)") == "From the environment variable FOLIO_PASSWORD"
    assert model.password_words("NOT FOUND (set it with ...)") == "Not stored yet"
    assert model.feed_state({"enabled": False, "last_status": "held"}) == "Paused, held for review"
    assert model.feed_state({"enabled": True}) == "On, no runs yet"      # "Running" is for an action under way
    summary = {"warnings": [{"level": "problem", "code": "feed-file", "text": "broken", "feed": "x"},
                            {"level": "warning", "code": "task-stale", "text": "late", "feed": None}],
               "problems": {"x": "broken"}, "recipe_problems": ["r.yaml: bad"]}
    assert model.warnings(summary) == ["Warning: late"]          # a level in words, not only a colour
    assert model.file_problems(summary) == ["x: broken", "r.yaml: bad"]


def test_the_page_s_words_for_feeds_and_runs():
    # A sentence naming a command the app has a button for is left out; one it has no button for stays.
    held = "Item count fell. Then send them with `folio-exchange release chq`. Or run `folio-exchange init`."
    assert model.page_words(held) == "Item count fell. Or run `folio-exchange init`."
    assert model.page_words("Run `folio-exchange run chq`.") == "Run `folio-exchange run chq`."   # never empty
    assert model.job_words("release", "chq") == "send of chq" and model.job_words("check", None) == "connection check"
    assert model.job_words("rename", None) == "rename"
    assert model.next_step("held", True).startswith("Look at the files (Open folder)")
    assert model.next_step("built", False) == "This feed has nowhere to send files yet, so they stay on this computer."
    assert model.next_step("success", True) is None
    assert model.size(512) == "512 B" and model.size(2048) == "2.0 KB" and model.num(None) == "–"
    assert model.num(12345) == "12,345"
    paused = {"code": "paused", "feed": "chq", "level": "note", "text": "Paused. `folio-exchange resume chq`"}
    assert model.warning_text(paused, True).startswith("This feed is paused")
    assert model.warning_text({"code": "recipe-update", "text": "x"}, newer="chq-academic@2.0.0").startswith(
        "A newer version of its recipe (chq-academic@2.0.0) is available.")
    assert model.setting_lines({"exclude_location_codes": ["A", "B"], "bibs_without_items": False}) == [
        "Locations left out (exclude_location_codes): A, B",
        "Include bibs that have no items (bibs_without_items): no"]
    assert model.detail_text({"from": "app", "files": ["a", "b"]}) == "From: app · Files: a, b"


def test_what_a_release_would_send_in_words():
    summary = {"built": "2026-10-27T10:00:00+00:00", "recipe": "chq-academic@1.0.0", "items": 5, "bibs": 4,
               "last_delivered_items": None, "reason": None, "destination": "folder D:\\drop"}
    rows = dict(model.release_rows(summary))
    assert rows["Items"] == "5 (nothing delivered there before)"
    assert rows["Waiting because"] == "it was built without sending"
    assert rows["Sends to"] == "folder D:\\drop"


def test_a_runs_summary_says_what_to_do_next():
    run = {"status": "blocked", "started": None, "recipe": "chq-academic@1.0.0", "folder": "20261027-1",
           "error": "Item count fell to 10% of the last delivery. Use `folio-exchange release chq`."}
    report = {"build": {"items_skipped": {"no barcode": 2}, "bibs_written": 4, "bibs_from_srs": 3,
                        "bibs_generated": 1}}
    rows = dict(model.run_summary(run, report, True))
    assert rows["Problem"] == "Item count fell to 10% of the last delivery."
    assert rows["Next step"].startswith("Look at the files (Open folder). If they are right, use Review and send")
    assert rows["Items left out"] == "no barcode: 2" and rows["Bibs"] == "4 (3 from MARC, 1 generated)"


def test_a_delivery_is_described_without_its_secret():
    d = {"transport": "sftp", "host": "h.example", "port": None, "username": "lib", "remote_dir": None,
         "host_key_fingerprint": "SHA256:" + "k" * 43, "host_key_confirmed": True, "auth": "password",
         "password": "system keyring (folio-exchange-sftp)"}
    rows = dict(model.delivery_rows(d))
    assert rows["Server"] == "h.example:22" and rows["Folder on the server"] == "."
    assert rows["Server key"].endswith("Confirmed: it matches the fingerprint the vendor gave.")
    assert rows["Login"] == "With a password. Stored in Windows Credential Manager (folio-exchange-sftp)."
    assert model.delivery_rows({"transport": "none"})[0][1].startswith("Nowhere yet")
    # Regression (parity-4): it sent people to a Change delivery button the app did not have (D3 brings it).
    unconfirmed = dict(model.delivery_rows({**d, "host_key_confirmed": False}))["Server key"]
    assert unconfirmed.endswith("enter the vendor's fingerprint under Change delivery… on this tab.")
    ok = {"status": "done", "result": {"where": "folder D:\\drop", "entries": None}}
    assert "usual for upload-only accounts" in model.delivery_check_words(ok)
    assert model.delivery_check_words({"status": "error", "error": "No answer"}) == "The test failed. No answer"


def test_feeds_that_need_a_person():
    snap = {"feeds": {
        "a": {"waiting": {"status": "held", "started": None, "held": "first delivery to folder X"}, "last_run": None},
        "b": {"waiting": None, "last_run": {"status": "failed", "started": None, "error": "FOLIO did not answer."}},
        "c": {"waiting": None, "last_run": {"status": "success"}, "overdue": "No scheduled run for 3 days."},
        "d": {"waiting": None, "last_run": {"status": "success"}}}}
    got = model.attention(snap)
    assert [f for f, _ in got] == ["a", "b", "c"]
    assert got[0][1] == "a: Held for review. The run of never is waiting for you: first delivery to folder X"
    assert got[1][1] == "b: the last run failed (never). FOLIO did not answer."
    assert got[2][1] == ("c: No scheduled run for 3 days. Check that the task that starts scheduled runs on this "
                         "computer is still set up and running.")


def test_an_overdue_feed_in_needs_you_names_no_command():
    """Regression (parity-3): Home showed the core's text, lower case and naming `run --due`."""
    snap = {"feeds": {"chq": {"waiting": None, "last_run": None, "overdue": (
        "overdue: last success 2026-10-20 03:30 UTC; check that the hourly `run --due` task is still running")}}}
    (_feed, line), = model.attention(snap)
    assert line == ("chq: Overdue: last success 2026-10-20 03:30 UTC. Check that the task that starts scheduled "
                    "runs on this computer is still set up and running.")


def test_main_without_qt_says_how_to_add_it(monkeypatch, capsys):
    for name in [m for m in sys.modules if m == "PySide6" or m.startswith("PySide6.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "PySide6", None)            # as if the desktop extra were not installed
    assert desktop.main(["--site", "x"]) == 1
    err = capsys.readouterr().err
    assert desktop.install_command() in err and "folio-exchange ui" in err


def test_the_way_to_add_qt_names_this_python_and_only_qt(monkeypatch, tmp_path):
    """Regression: the app told people to run pip install "folio-exchange[desktop]", but FOLIO-Exchange is not
    on PyPI, so that would install whatever someone else put there under its name, and into whichever Python
    pip belonged to."""
    command = desktop.install_command()
    assert command.startswith(f'"{Path(sys.executable)}" -m pip install ') or "pythonw" in sys.executable.lower()
    assert command.endswith(f' -m pip install "{desktop.QT}"')         # Qt alone, never FOLIO-Exchange
    pins = (Path(__file__).parents[1] / "constraints.txt").read_text(encoding="utf-8").splitlines()
    assert desktop.QT in pins                              # the Qt the tests pass with
    for message in (desktop.NOT_INSTALLED.format(install=command),
                    desktop.NOT_LOADED.format(error="DLL load failed", install=command)):
        assert command in message and "folio-exchange[desktop]" not in message
    (tmp_path / "python.exe").write_bytes(b"")
    (tmp_path / "pythonw.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "pythonw.exe"))
    assert desktop.install_command().startswith(f'"{tmp_path / "python.exe"}" -m pip')    # pip's output shows
    help_text = cli.build_parser().format_help()
    assert "folio-exchange[desktop]" not in help_text


def test_main_without_a_console_uses_a_message_box(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    shown = []
    monkeypatch.setattr(desktop, "qt_problem", lambda: "no Qt here")
    if sys.platform == "win32":
        import ctypes

        monkeypatch.setattr(ctypes.windll.user32, "MessageBoxW", lambda _h, text, title, _f: shown.append(text))
    assert desktop.main([]) == 1
    assert sys.stdout is not None and sys.stderr is not None     # printing is safe from here on
    if sys.platform == "win32":
        assert shown == ["no Qt here"]


def test_help_without_a_console_is_shown_in_a_message_box(monkeypatch):
    """Regression: under pythonw, --help printed into nothing and the app quietly exited."""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    shown = []
    monkeypatch.setattr(desktop, "tell", lambda message, console, icon=desktop.MB_ICONERROR:
                        shown.append((message, console, icon)))
    with pytest.raises(SystemExit) as done:
        desktop.main(["--help"])
    assert done.value.code == 0
    assert len(shown) == 1 and "usage: folio-exchange-app" in shown[0][0]
    assert shown[0][1:] == (False, desktop.MB_ICONINFORMATION)


def test_ci_stops_when_qt_does_not_load_rather_than_skip_the_widget_tests():
    """Regression: a Qt that failed to load on CI (a Linux library missing) skipped every widget test, and the
    run still passed."""
    from conftest import require_qt_if_asked

    with pytest.raises(pytest.exit.Exception, match="cannot run: ImportError"):
        require_qt_if_asked("ImportError: libEGL.so.1", {"FX_REQUIRE_QT": "1"})
    require_qt_if_asked("ImportError: libEGL.so.1", {})            # a developer without Qt: the tests skip
    require_qt_if_asked(None, {"FX_REQUIRE_QT": "1"})


def test_a_usage_mistake_is_said_plainly(capsys):
    assert desktop.main(["--nonsense"]) == 1
    assert "unrecognized arguments" in capsys.readouterr().err


def test_folio_exchange_app_passes_the_site_through(monkeypatch, tmp_path):
    got = []
    monkeypatch.setattr(desktop, "main", lambda argv=None: got.append(argv) or 0)
    assert cli.main(["--site", str(tmp_path), "app"]) == 0
    assert cli.main(["app"]) == 0
    assert cli.main(["-v", "--site", str(tmp_path), "app"]) == 0
    assert got == [["--site", str(tmp_path)], [], ["--site", str(tmp_path), "--verbose"]]


def test_the_command_line_does_not_load_qt():
    import subprocess

    code = ("import sys, folio_exchange.cli, folio_exchange.ui, folio_exchange.desktop, "
            "folio_exchange.desktop.places, folio_exchange.desktop.model; "
            "print(any(m.startswith('PySide6') for m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         cwd=Path(__file__).parent)
    assert out.stdout.strip() == "False"
