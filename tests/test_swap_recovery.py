"""The in-place updater's recovery, before the release key is set (review of PR 38): renames tried again, a
rollback that fails saying plainly what's where (and in a message box when no program is left to say it), a
program.outgoing left by an update that worked tidied rather than blocking every later one, and a site that can't
be reached stopping the update while a folder with no site in it is skipped and said. PR 36's fixtures: a faked
GitHub, a throwaway key, a temporary install folder. Nothing real is installed or renamed."""
# Fixtures are imported from test_selfupdate, and test functions take them by name.
# ruff: noqa: F811
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest
from sitekit import make_site
from test_selfupdate import (  # noqa: F401
    CALM,
    NEW,
    OLD,
    FakeGitHub,
    Runner,
    fake_env,
    install,
    key,
    ready,
    release,
    run_prepare,
)

from folio_exchange import selfupdate, swaphelper
from folio_exchange.desktop import installing


def updates_dir(install) -> Path:
    return Path(install["root"]).parent / "app" / "updates"


# ------------------------------------------------------------------ renames tried again
def test_a_rename_held_for_a_moment_is_tried_again(install, ready):
    """An antivirus scan holding a file for a moment mustn't stop the swap."""
    tries, slept = {}, []

    def rename(src, dst):
        n = tries[Path(src).name] = tries.get(Path(src).name, 0) + 1
        if Path(src).name == "program.new" and n < 3:
            raise PermissionError(32, "The process cannot access the file")
        os.rename(src, dst)

    result = selfupdate.swap(ready, run=Runner(), rename=rename, processes=lambda f: [], now=lambda: CALM,
                             sleep=slept.append)
    assert result["ok"] and result["program"] == "new" and tries["program.new"] == 3
    assert slept.count(swaphelper.RENAME_WAIT) == 2


def test_a_rollback_held_for_a_moment_still_puts_the_old_program_back(install, ready):
    tries = {}

    def rename(src, dst):
        pair = (Path(src).name, Path(dst).name)
        tries[pair] = tries.get(pair, 0) + 1
        if pair == ("program.new", "program"):
            raise PermissionError(5, "Access is denied")
        if pair == ("program.outgoing", "program") and tries[pair] < 2:
            raise PermissionError(32, "The process cannot access the file")
        os.rename(src, dst)

    result = selfupdate.swap(ready, run=Runner(), rename=rename, processes=lambda f: [], now=lambda: CALM,
                             sleep=lambda s: None)
    assert result["program"] == "old" and result["reason"].endswith("The old version was put back.")
    assert (Path(install["root"]) / "program" / "VERSION").read_text(encoding="utf-8") == OLD


# ------------------------------------------------------------------ a rollback that fails says what's where
def test_a_failed_rollback_with_the_new_version_in_place_says_so(install, ready):
    def rename(src, dst):
        if Path(dst).name == "program.failed":
            raise PermissionError(5, "Access is denied")
        os.rename(src, dst)

    result = selfupdate.swap(ready, run=Runner(broken={"program"}), rename=rename, processes=lambda f: [],
                             now=lambda: CALM, sleep=lambda s: None)
    root = Path(install["root"])
    assert result["ok"] is False and result["program"] == "new"
    assert f"FOLIO-Exchange {NEW} is in {root / 'program'}, though it didn't pass its check." in result["done"][0]
    assert not any("still installed" in d for d in result["done"])
    assert result["left"][0].startswith("Run the installer again, or rename")
    result["finished"] = CALM.isoformat()
    folder = updates_dir(install)
    (folder / selfupdate.RESULT).write_text(json.dumps(result), encoding="utf-8")
    line = installing.last_update(updates_dir=folder, now=CALM, running=NEW)
    assert line.startswith(f"The update to {NEW} didn't finish. FOLIO-Exchange {NEW} is in") and "still" not in line


def test_no_program_left_is_said_and_shown_in_a_message_box(install, ready):
    def rename(src, dst):
        if Path(src).name == "program.new" or (Path(src).name, Path(dst).name) == ("program.outgoing", "program"):
            raise PermissionError(5, "Access is denied")
        os.rename(src, dst)

    result = selfupdate.swap(ready, run=Runner(), rename=rename, processes=lambda f: [], now=lambda: CALM,
                             sleep=lambda s: None)
    root = Path(install["root"])
    assert result["program"] == "none"
    assert f"No version of FOLIO-Exchange is in {root / 'program'} now." in result["done"][0]
    assert result["left"][0].startswith(f"Rename {root / 'program.outgoing'} to program, or run the installer again.")


def test_the_helper_tells_the_person_when_no_program_is_left(install, ready, monkeypatch):
    """With every window closed and no program to start, only the helper can say it."""
    plan = json.loads(Path(ready).read_text(encoding="utf-8"))
    gone = {"ok": False, "version": NEW, "from": OLD, "program": "none", "reason": "The swap stopped halfway.",
            "done": ["No version of FOLIO-Exchange is in the program folder now."], "left": ["Run the installer."]}
    monkeypatch.setattr(swaphelper, "swap", lambda plan: dict(gone))
    told = []
    out = Path(plan["result"])
    assert swaphelper.main([ready, plan["root"], str(out)], tell=told.append) == 1
    assert told == ["The swap stopped halfway. No version of FOLIO-Exchange is in the program folder now. Run the "
                    "installer."]
    assert json.loads(out.read_text(encoding="utf-8"))["program"] == "none"
    monkeypatch.setattr(swaphelper, "swap", lambda plan: {**gone, "program": "old"})
    told.clear()
    swaphelper.main([ready, plan["root"], str(out)], tell=told.append)
    assert told == []                                              # a window comes back to say it


# ------------------------------------------------------------------ program.outgoing left by an update that worked
def write_last(install, **result):
    folder = updates_dir(install)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / selfupdate.RESULT).write_text(json.dumps({"finished": CALM.isoformat(), **result}), encoding="utf-8")


def test_an_outgoing_folder_left_by_an_update_that_worked_is_kept_as_previous(install, key):
    root = Path(install["root"])
    fake_env(root / "program.outgoing", "0.1.0")
    write_last(install, ok=True, program="new", version=OLD, **{"from": "0.1.0"})
    result = run_prepare(install, key, FakeGitHub(release(key[0])))
    assert result["ok"], result["reason"]
    assert not (root / "program.outgoing").exists()
    assert (root / "program.previous" / "VERSION").read_text(encoding="utf-8") == "0.1.0"
    assert any("is now" in d and "program.previous" in d for d in result["done"])


@pytest.mark.parametrize("last", [None, {"ok": False, "program": "old"}, {"ok": False, "program": "new"}])
def test_an_outgoing_folder_from_an_update_that_did_not_finish_still_refuses(install, key, last):
    root = Path(install["root"])
    fake_env(root / "program.outgoing", "0.1.0")
    if last is not None:
        write_last(install, version=OLD, **{"from": "0.1.0"}, **last)
    result = run_prepare(install, key, FakeGitHub(release(key[0])))
    assert result["ok"] is False and result["stage"] == "busy"
    assert result["reason"].startswith("An earlier update didn't finish")
    assert (root / "program.outgoing").is_dir()                    # it may hold the only working version


# ------------------------------------------------------------------ sites that can't be reached
def test_a_site_that_cannot_be_reached_stops_the_update(install, key, tmp_path):
    """Its drive or share isn't connected: it may well hold a site, so the update doesn't go on without its copy
    (docs/updates-plan.md: a site that can't be copied stops the update)."""
    offline = tmp_path / "offline-share" / "Lakeside"
    result = run_prepare(install, key, FakeGitHub(release(key[0])), sites=[offline])
    assert result["ok"] is False and result["stage"] == "copy"
    assert result["reason"] == (f"The site {offline} can't be reached, so it couldn't be copied. Nothing was "
                                "updated. Connect the drive or network share it's on, then try again.")


def test_an_unreachable_site_that_is_only_recent_is_skipped_and_named(install, key, tmp_path, caplog):
    """Regression (re-review of PR 38): the recent list keeps ten folders, roams, and the app can't drop one, so a
    site once opened from a USB stick or an old share stopped every Install. Only the asking window's site and the
    tray's must be copied; a recent one that can't be reached is skipped and named."""
    stick = tmp_path / "usb-stick" / "Lakeside"
    with caplog.at_level(logging.INFO, logger="folio_exchange"):
        result = run_prepare(install, key, FakeGitHub(release(key[0])), sites=[stick], required=[])
    assert result["ok"], result["reason"]
    assert (f"{stick} wasn't copied, because it can't be reached. It's only on the list of recent sites."
            in result["done"])
    assert (f"The site {stick} can't be reached, so it isn't copied. It's only on the list of recent sites, so the "
            "update goes on." in [r.getMessage() for r in caplog.records])


def test_an_unreachable_site_the_tray_watches_still_stops_the_update(install, key, tmp_path, monkeypatch):
    """With no list given, the engine takes the tray's sites as the ones that must be copied."""
    from folio_exchange.desktop import places

    watched, recent = tmp_path / "share-a" / "Lakeside", tmp_path / "share-b" / "Hillside"
    places.watch_site(watched, True)
    monkeypatch.setattr(selfupdate, "known_sites", lambda: [recent, watched])
    result = run_prepare(install, key, FakeGitHub(release(key[0])), sites=None)
    assert result["ok"] is False and result["stage"] == "copy"
    assert result["reason"].startswith(f"The site {watched} can't be reached")
    places.watch_site(watched, False)
    assert run_prepare(install, key, FakeGitHub(release(key[0])), sites=None)["ok"]   # the recent one alone


def test_a_folder_with_no_site_in_it_is_skipped_and_said(install, key, tmp_path, caplog):
    site = make_site(tmp_path / "sites" / "lakeside", "https://folio.example.org")
    deleted = tmp_path / "sites" / "deleted"                       # gone, from a folder that's there
    empty = tmp_path / "sites" / "unfinished"
    empty.mkdir()
    with caplog.at_level(logging.INFO, logger="folio_exchange"):
        result = run_prepare(install, key, FakeGitHub(release(key[0])), sites=[site, deleted, empty])
    assert result["ok"], result["reason"]
    said = [r.getMessage() for r in caplog.records if r.name == "folio_exchange.selfupdate"]
    assert f"Copying the site {site}…" in said
    for folder in (deleted, empty):
        assert f"{folder} isn't copied, because there's no site there now." in said
        assert f"{folder} wasn't copied, because there's no site there now." in result["done"]
    assert not any(f"Copying the site {deleted}" in s for s in said)
