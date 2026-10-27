"""The double-click launcher (`folio-exchange launcher`) and how the page shares its port."""
import json
import os
import socket
import subprocess
import sys

import pytest

from folio_exchange import launcher, ui
from folio_exchange.cli import main
from folio_exchange.errors import ConfigError, FxError

AWKWARD = "my lib (é) 100% & co"


def test_the_windows_launcher_quotes_both_paths_and_escapes_percent():
    text = launcher.script(r"C:\FOLIO-Exchange\100% & co", r"C:\Program Files\fx\folio-exchange.exe", "Windows")
    assert text.endswith("\r\n") and "\n" not in text.replace("\r\n", "")
    assert '"C:\\Program Files\\fx\\folio-exchange.exe" --site "C:\\FOLIO-Exchange\\100%% & co" ui\r\n' in text
    assert launcher.MARKER in text and "if errorlevel 1 pause" in text
    assert "chcp" not in text                                     # plain ASCII needs no code page change
    assert "chcp 65001" in launcher.script(r"C:\Bibliothèque", "fx.exe", "Windows")


def test_a_site_at_a_drive_root_keeps_its_closing_quote():
    assert '--site "C:\\\\" ui' in launcher.script("C:\\", "fx.exe", "Windows")


def test_the_posix_launcher_quotes_for_the_shell():
    text = launcher.script("/srv/my lib's site", "/opt/fx/bin/folio-exchange", "Linux")
    assert text.startswith("#!/bin/sh\n")
    assert "exec /opt/fx/bin/folio-exchange --site '/srv/my lib'\"'\"'s site' ui\n" in text
    assert launcher.file_name("/srv/mylib", "Darwin") == "FOLIO-Exchange (mylib).command"
    assert launcher.file_name("/srv/mylib", "Linux") == "FOLIO-Exchange (mylib).sh"


def test_write_replaces_its_own_launcher_but_never_another_file(tmp_path):
    site = tmp_path / "mylib"
    path = launcher.write(site, tmp_path, "fx.exe", "Windows")
    assert path.name == "FOLIO-Exchange (mylib).cmd"
    assert launcher.write(site, tmp_path, "other.exe", "Windows") == path
    assert '"other.exe"' in path.read_text(encoding="utf-8")
    path.write_bytes(b"@echo my own script\r\n")
    with pytest.raises(ConfigError, match="was not made by folio-exchange"):
        launcher.write(site, tmp_path, "fx.exe", "Windows")
    assert path.read_bytes() == b"@echo my own script\r\n"


@pytest.mark.skipif(os.name == "nt", reason="file modes are POSIX")
def test_the_posix_launcher_can_be_run(tmp_path):
    path = launcher.write(tmp_path / "mylib", tmp_path, "fx", "Linux")
    assert path.stat().st_mode & 0o111


@pytest.mark.skipif(os.name != "nt", reason="runs a .cmd file in Command Prompt")
def test_the_cmd_launcher_hands_the_program_its_arguments_intact(tmp_path):
    printer = tmp_path / "printer.py"
    printer.write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8")
    fake = tmp_path / "fake fx.cmd"                              # stands in for folio-exchange.exe
    fake.write_bytes(f'@"{sys.executable}" "{printer}" %*\r\n'.encode())
    site = tmp_path / AWKWARD
    path = launcher.write(site, tmp_path, str(fake), "Windows")
    assert path.name == "FOLIO-Exchange (my lib (é) 100 co).cmd"
    # as Explorer starts a .cmd: the outer quotes keep cmd /c from splitting the path at its spaces
    out = subprocess.run(f'cmd /d /c ""{path}""', capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
    last = out.stdout.decode("utf-8", "replace").strip().splitlines()[-1]
    assert json.loads(last) == ["--site", str(site), "ui"]


def test_the_command_writes_into_the_folder_given(site, tmp_path, capsys):
    folder = tmp_path / "launchers"
    folder.mkdir()
    assert main(["-s", str(site), "launcher", "--folder", str(folder)]) == 0
    made = list(folder.iterdir())
    assert len(made) == 1 and made[0].name.startswith("FOLIO-Exchange (site)")
    assert str(site) in made[0].read_text(encoding="utf-8")
    assert "Double-click it" in capsys.readouterr().out
    assert main(["-s", str(site), "launcher", "--folder", str(tmp_path / "nope")]) == 1


def test_the_launcher_names_this_install(monkeypatch, tmp_path):
    exe = tmp_path / ("folio-exchange.exe" if os.name == "nt" else "folio-exchange")
    exe.write_text("")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python"))
    assert launcher.program_path() == str(exe)


# ------------------------------------------------------------------ the page's port
def test_a_second_page_cannot_take_a_port_that_is_in_use(site):
    httpd, app = ui.start_in_thread(site)
    try:
        with pytest.raises(OSError):
            ui._Server(("127.0.0.1", app.port), ui.make_handler(app))
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_opening_the_page_twice_reuses_the_open_one(site, capsys, monkeypatch):
    opened = []
    monkeypatch.setattr(ui.webbrowser, "open", opened.append)
    monkeypatch.delenv("FOLIO_EXCHANGE_NO_BROWSER", raising=False)
    httpd, app = ui.start_in_thread(site)
    try:
        ui.serve(site, port=app.port)                            # returns at once instead of serving
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert "already open" in capsys.readouterr().out
    assert opened == [f"http://127.0.0.1:{app.port}/"]


def test_another_program_on_the_default_port_moves_the_page_to_a_free_one(site, capsys, monkeypatch):
    other = socket.socket()
    other.bind(("127.0.0.1", 0))
    other.listen()
    monkeypatch.setattr(ui, "DEFAULT_PORT", other.getsockname()[1])
    monkeypatch.setattr(ui._Server, "serve_forever", lambda self, *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    try:
        ui.serve(site, open_browser=False)
        with pytest.raises(FxError, match="Could not listen"):
            ui.serve(site, port=other.getsockname()[1], open_browser=False)
    finally:
        other.close()
    out = capsys.readouterr().out
    assert "is in use by another program, so the page uses port" in out
