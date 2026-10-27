"""Batch 2.2 of the improvement plan: the command line (OPS-5, OPS-6, OPS-9, OPS-10 in docs/review-2026-10-27.md)."""
import json
import logging
from pathlib import Path

import pytest
from sitekit import FOLIO_TEST_PASSWORD, make_feed

from folio_exchange import cli, delivery, schedule
from folio_exchange.cli import main
from folio_exchange.wizard import ScriptedPrompter, run_wizard

NONE = {"transport": "none", "directory": None}


def dry_run_site(site: Path) -> Path:
    make_feed(site, delivery=NONE)
    return site


def latest_report(site: Path) -> dict:
    run = sorted(p for p in (site / "out" / "chq").iterdir() if p.name[:1].isdigit())[-1]
    return json.loads((run / "run-report.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ OPS-6: dry runs (transport none)
def test_a_dry_run_feed_gets_its_own_next_steps_after_run_and_build(site, capsys):
    dry_run_site(site)
    assert main(["-s", str(site), "run", "chq"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "success"                       # the documented status is kept
    assert "Dry run: nothing was sent" in out["next"] and "folio-exchange init" in out["next"]
    assert str(site / "out" / "chq") in out["next"] and "release" not in out["next"]
    assert latest_report(site)["next"] == out["next"]
    assert main(["-s", str(site), "build", "chq"]) == 0
    built = json.loads(capsys.readouterr().out)
    assert built["status"] == "built" and "Dry run" in built["next"]


def test_release_refuses_a_feed_without_delivery(site, caplog):
    dry_run_site(site)
    assert main(["-s", str(site), "run", "chq", "--no-deliver"]) == 0
    with caplog.at_level(logging.ERROR):
        assert main(["-s", str(site), "release", "chq", "--yes"]) == 1
    assert "has no delivery set up, so there is nothing to send" in caplog.text
    assert "folio-exchange init" in caplog.text
    assert latest_report(site)["status"] == "built"          # nothing was marked as delivered


def test_history_and_feeds_say_a_dry_run_sent_nothing(site, capsys):
    dry_run_site(site)
    assert main(["-s", str(site), "feeds"]) == 0
    assert "folio-exchange run chq --no-deliver" in capsys.readouterr().out
    assert main(["-s", str(site), "run", "chq"]) == 0
    capsys.readouterr()
    main(["-s", str(site), "history"])
    assert "not sent: transport none" in capsys.readouterr().out
    main(["-s", str(site), "history", "--show", "1"])
    assert "success (not sent: transport none)" in capsys.readouterr().out
    main(["-s", str(site), "feeds"])
    out = capsys.readouterr().out
    assert "success (not sent: transport none)" in out and "Dry run: nothing was sent" in out


def test_a_real_delivery_is_not_marked_as_not_sent(site, capsys):
    assert main(["-s", str(site), "run", "chq"]) == 2              # first delivery held
    assert main(["-s", str(site), "release", "chq", "--yes"]) == 0
    capsys.readouterr()
    main(["-s", str(site), "history"])
    assert "not sent" not in capsys.readouterr().out


def test_check_delivery_explains_a_dry_run_feed(site, capsys):
    dry_run_site(site)
    assert main(["-s", str(site), "check", "--delivery", "chq"]) == 0
    out = capsys.readouterr().out
    assert "no delivery set up (a dry run)" in out and "folio-exchange init" in out


def test_the_wizard_gives_a_dry_run_its_own_next_steps(tmp_path, folio, monkeypatch):
    monkeypatch.setenv("FOLIO_PASSWORD", FOLIO_TEST_PASSWORD)
    p = ScriptedPrompter([folio.url, "diku", "fx_reader", None, None, [], [], False, False, "none", "manual",
                          "none"])
    assert run_wizard(tmp_path / "site", p) == 0, "\n".join(p.transcript)
    text = "\n".join(p.transcript)
    closing = text[text.index("Saved site.yaml"):]
    assert "Next steps for a dry run" in closing
    for step in (" check ", " preview chq ", " run chq --no-deliver ", " history "):
        assert step in closing
    assert "release chq" not in closing and "run init again" in closing


# ------------------------------------------------------------------ OPS-5: paused feeds
def test_check_delivery_tells_a_paused_feed_how_to_resume(site, capsys):
    (site / "vendor-drop").mkdir()
    assert main(["-s", str(site), "feeds", "pause", "chq"]) == 0
    capsys.readouterr()
    assert main(["-s", str(site), "check", "--delivery", "chq"]) == 0
    out = capsys.readouterr().out
    assert "[paused]" in out and "Paused: the test passed" in out and "feeds resume chq" in out


def test_check_delivery_keeps_a_failing_paused_feed_paused(site, capsys, monkeypatch):
    def fail(d):
        raise OSError("no route")

    monkeypatch.setattr(delivery, "check_delivery", fail)
    assert main(["-s", str(site), "feeds", "pause", "chq"]) == 0
    capsys.readouterr()
    assert main(["-s", str(site), "check", "--delivery", "chq"]) == 1
    out = capsys.readouterr().out
    assert "FAILED" in out and "leave it paused until this test passes" in out and "feeds resume" not in out


def test_feeds_names_resume_for_a_paused_feed(site, capsys):
    assert main(["-s", str(site), "feeds", "pause", "chq"]) == 0
    capsys.readouterr()
    main(["-s", str(site), "feeds"])
    out = capsys.readouterr().out
    assert "check --delivery chq" in out and "folio-exchange feeds resume chq" in out


def test_the_wizard_names_check_delivery_for_a_paused_feed(tmp_path, folio, monkeypatch):
    monkeypatch.setenv("FOLIO_PASSWORD", FOLIO_TEST_PASSWORD)
    monkeypatch.setattr(delivery, "check_delivery", lambda d: [])
    p = ScriptedPrompter([folio.url, "diku", "fx_reader", None, None, [], [], False, False, "sftp", "h.example",
                          None, "u", None, "unconfirmed", None, None, "none"])
    assert run_wizard(tmp_path / "site", p, read_host_key=lambda h, port: "SHA256:" + "k" * 43) == 0
    text = "\n".join(p.transcript)
    assert "check --delivery chq" in text and "feeds resume chq" in text
    # check --delivery refuses an unconfirmed key, so the key is confirmed with init first
    assert "run init again to confirm it" in text


# ------------------------------------------------------------------ OPS-9: the scheduled task
EXE = r"C:\Program Files\FOLIO Exchange\venv\Scripts\folio-exchange.exe"


def _ps_strings(line: str) -> list[str]:
    """The single-quoted PowerShell string literals in a line, unquoted the way PowerShell's tokenizer does it:
    ' and the typographic single quotes all open and close a literal, and two of them in a row stand for the
    second one."""
    quotes, out, i = "'‘’‚‛", [], 0
    while i < len(line):
        if line[i] not in quotes:
            i += 1
            continue
        i, text = i + 1, ""
        while True:
            c = line[i]
            if c in quotes:
                if line[i + 1:i + 2] == "" or line[i + 1] not in quotes:
                    break
                i += 1
                c = line[i]
            text, i = text + c, i + 1
        out.append(text)
        i += 1
    return out


def test_the_manual_windows_task_has_the_same_settings_as_schedule_register(tmp_path):
    site = tmp_path / "my library"
    text = schedule.os_command(site, EXE, "Windows", "LIB\\fx")
    assert "schtasks" not in text and "cmd --%" not in text          # /TR leaves Task Scheduler's defaults
    for switch in ("-AllowStartIfOnBatteries", "-DontStopIfGoingOnBatteries", "-StartWhenAvailable",
                   "-ExecutionTimeLimit (New-TimeSpan -Hours 12)", "-RepetitionInterval (New-TimeSpan -Hours 1)",
                   "-At '2020-01-01 00:07'", "-MultipleInstances IgnoreNew"):
        assert switch in text
    assert schedule.TASK_TIME_LIMIT == "PT12H"
    lines = text.splitlines()
    assert lines[-1].startswith(f"Register-ScheduledTask -TaskName '{schedule.task_name(site.resolve())}' ")
    assert lines[-1].endswith(" -Force") and "-User $cred.UserName" in lines[-1]
    assert "Get-Credential -UserName 'LIB\\fx'" in text               # the password is asked for, never typed in
    assert "/RU" not in text and "/RP" not in text
    without = schedule.os_command(site, EXE, "Windows")
    assert "Get-Credential" not in without and "-User" not in without


def test_the_schedule_register_line_has_a_powershell_and_a_command_prompt_form(tmp_path):
    site = tmp_path / "it's a library"
    ps = schedule.register_command(site, EXE, "powershell")
    cmd = schedule.register_command(site, EXE, "cmd")
    assert ps.startswith("& '") and ps.endswith(" schedule --register")
    assert _ps_strings(ps) == [EXE, str(site.resolve())]
    assert _crt_args(cmd) == [EXE, "--site", str(site.resolve()), "schedule", "--register"]


def test_win_quote_keeps_a_trailing_backslash_from_eating_the_quote():
    assert schedule.win_quote("C:\\") == '"C:\\\\"'
    assert schedule.win_quote('a "b" c') == '"a \\"b\\" c"'
    assert schedule.win_quote("C:\\my library") == '"C:\\my library"'


def _crt_args(line: str) -> list[str]:
    """Split a Windows command line the way the C runtime does (enough for these tests)."""
    args, cur, quoted, i, started = [], "", False, 0, False
    while i < len(line):
        c = line[i]
        if c == "\\":
            n = len(line[i:]) - len(line[i:].lstrip("\\"))
            if i + n < len(line) and line[i + n] == '"':
                cur += "\\" * (n // 2)
                if n % 2:
                    cur += '"'
                    i += n + 1
                else:
                    i += n
                started = True
                continue
            cur += "\\" * n
            i += n
            started = True
            continue
        if c == '"':
            quoted, started = not quoted, True
        elif c == " " and not quoted:
            if started:
                args.append(cur)
            cur, started = "", False
        else:
            cur, started = cur + c, True
        i += 1
    if started:
        args.append(cur)
    return args


@pytest.mark.parametrize("site", ["C:/FOLIO-Exchange/my library (main)", "C:/", "C:/O\u2019Brien's library"])
def test_the_manual_task_receives_the_task_action_intact(site, monkeypatch):
    monkeypatch.setattr(schedule.Path, "resolve", lambda self: self)    # Windows paths on any test machine
    site_path = Path(site)
    text = schedule.os_command(site_path, EXE, "Windows", "LIB\\fx")
    action = next(line for line in text.splitlines() if line.startswith("$action = "))
    command, arguments = _ps_strings(action)
    assert (command, arguments) == schedule.windows_task_action(site_path, EXE)
    assert _crt_args(f"{command} {arguments}") == [EXE, "--site", str(site_path), "run", "--due"]
    register = text.splitlines()[-1]
    assert _ps_strings(register)[0] == f"FOLIO-Exchange ({site_path.name or 'C'})"   # never "()"


def test_schedule_prints_schedule_register_and_the_manual_task(site, capsys, monkeypatch):
    monkeypatch.setattr(cli.platform, "system", lambda: "Windows")
    assert main(["-s", str(site), "schedule"]) == 0
    out = capsys.readouterr().out
    assert "In PowerShell" in out and "Command Prompt (cmd.exe)" in out
    assert out.count("schedule --register") == 2 and "& '" in out
    assert "Register-ScheduledTask" in out and "-AllowStartIfOnBatteries" in out
    assert "schtasks /Create" not in out and "cmd --%" not in out
    assert "schtasks /Query" in out and "Task To Run" in out


# ------------------------------------------------------------------ OPS-10: wording
def test_upgrade_help_says_it_does_not_update_the_program(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    assert "not an update of FOLIO-Exchange itself" in capsys.readouterr().out
