"""Installing an update from the app (desktop/updating.py), without Qt: whether Options offers it, the order of
the steps, closing the other windows and the tray and starting them again, the line Options shows afterwards,
and the audit entry. The engine and the sockets are stand-ins; the real engine runs only against PR 36's
temporary install folder (test_selfupdate's fixtures). Nothing goes online and no real key is used."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sitekit import make_site
from test_selfupdate import CALM, NEW, OLD, Builder, FakeGitHub, Runner, install, key, make_key, release  # noqa: F401

from folio_exchange import selfupdate
from folio_exchange.config import load_site
from folio_exchange.desktop import places, startup, updating
from folio_exchange.history import History

STATUS = {"mode": "on", "available": {"version": NEW, "notes": "Fixes.", "page": "https://example.org"}}
UPDATABLE = {"updatable": True, "root": "x", "program": "x/program", "python": "py", "reason": None, "command": None}


# ------------------------------------------------------------------ stand-ins
class Engine:
    """Stands in for selfupdate in updating.install: records what it's asked, and answers as told."""

    def __init__(self, prepared=None, started=None, sites=()):
        self.calls: list[tuple] = []
        self.prepared = prepared or {"ok": True, "stage": "ready", "program": "old", "plan": "plan.json",
                                     "done": [f"Downloaded FOLIO-Exchange {NEW} and checked its signature."],
                                     "left": [], "reason": None}
        self.started = started or {"ok": True, "stage": "swap", "program": "old", "done": [], "left": [],
                                   "reason": None}
        self.sites = list(sites)

    def known_sites(self):
        return list(self.sites)

    def build_environment(self, *args, **kw):
        self.calls.append(("build",))

    def prepare(self, tag, **kw):
        kw["sites"] = list(kw["sites"])
        self.calls.append(("prepare", tag, kw))
        return dict(self.prepared)

    def start_swap(self, plan, restart=()):
        self.calls.append(("start_swap", plan, [list(a) for a in restart]))
        return dict(self.started)

    def asked(self, what):
        return [c for c in self.calls if c[0] == what]


class Untouchable:
    def __getattr__(self, name):
        raise AssertionError(f"the engine was reached ({name}) without a key")


class Sockets:
    """Stands in for single.probe and single.ask_to_quit: which names answer, with which pid, and what each says
    when asked to quit (a list, its last answer repeated)."""

    def __init__(self, open_=None, answers=None):
        self.open, self.answers, self.asked = dict(open_ or {}), {k: list(v) for k, v in (answers or {}).items()}, []

    def probe(self, name):
        return self.open.get(name)

    def ask(self, name):
        self.asked.append(name)
        said = self.answers.get(name, ["quit"])
        return said.pop(0) if len(said) > 1 else said[0]


def run(own, engine, sockets=None, *, now=None, sleeps=None, launched=None, **kw):
    sockets = sockets or Sockets()
    sleeps = [] if sleeps is None else sleeps
    launched = [] if launched is None else launched
    return updating.install(NEW, key=b"k" * 32, own=own, engine=engine, probe=sockets.probe, ask_quit=sockets.ask,
                            launch=launched.append, sleep=sleeps.append, now=now or (lambda: CALM),
                            http=FakeGitHub({}), **kw)


def argv(command):
    exe, args, _folder = command
    return [exe, *args]


# ------------------------------------------------------------------ whether Options offers it
def test_offer_only_for_an_install_it_can_update_with_a_key_in_on_mode():
    assert updating.offer(STATUS, UPDATABLE, b"k" * 32) == {"install": True, "why": None}


def test_no_key_means_no_install_and_says_why():
    assert updating.offer(STATUS, UPDATABLE, None) == {"install": False, "why": updating.NO_KEY}


@pytest.mark.parametrize("mode", ["notify", "off"])
def test_notify_and_off_never_offer_it(mode):
    assert updating.offer({**STATUS, "mode": mode}, UPDATABLE, b"k" * 32) == {"install": False, "why": None}


def test_nothing_newer_offers_nothing():
    assert updating.offer({**STATUS, "available": None}, UPDATABLE, b"k" * 32)["install"] is False


def test_a_pip_install_is_told_why_in_plain_words(tmp_path):
    pip = selfupdate.find_install(prefix=tmp_path, base_prefix=tmp_path, version=NEW)
    got = updating.offer(STATUS, pip, b"k" * 32)
    assert got == {"install": False, "why": "FOLIO-Exchange was installed with pip, so it can't update itself."}


def test_an_editable_install_is_told_why(tmp_path):
    program = tmp_path / "program"
    program.mkdir()
    (program / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
    editable = selfupdate.find_install(prefix=program, base_prefix=tmp_path, package_dir=tmp_path / "src",
                                       version=NEW)
    why = updating.offer(STATUS, editable, b"k" * 32)["why"]
    assert why.startswith("This copy of FOLIO-Exchange runs from") and why.endswith("so it can't update itself.")


def test_the_install_is_looked_up_only_when_it_matters():
    asked = []
    updating.offer({**STATUS, "mode": "notify"}, None, b"k", find=lambda **kw: asked.append(kw) or UPDATABLE)
    assert asked == []
    assert updating.offer(STATUS, None, b"k", find=lambda **kw: asked.append(kw) or UPDATABLE)["install"]
    assert asked == [{"version": NEW}]


def test_the_release_key_comes_only_from_the_code():
    """updates.RELEASE_KEY is the one key the app passes: nothing else sets it, and only the window's Install
    reads it (no setting, file or environment variable)."""
    import ast

    import folio_exchange

    src = Path(folio_exchange.__file__).parent
    readers, setters = set(), []
    for path in src.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            named = (node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute)
                     else None)
            if named == "RELEASE_KEY":
                where = path.relative_to(src).as_posix()
                if isinstance(node.ctx, ast.Load):
                    readers.add(where)
                else:
                    setters.append((where, node.lineno))
    assert readers == {"desktop/actions.py"}
    assert [where for where, _line in setters] == ["updates.py"]                    # its definition, only


# ------------------------------------------------------------------ installing
def test_without_a_key_the_engine_is_never_reached(tmp_path):
    res = updating.install(NEW, key=None, own=tmp_path, engine=Untouchable(), probe=pytest.fail, ask_quit=pytest.fail)
    assert res["ok"] is False and res["stage"] == "key" and res["reason"].startswith(updating.NO_KEY)


def test_a_yes_prepares_the_tag_with_the_key_closes_the_others_and_starts_the_swap(tmp_path, caplog):
    own, other = tmp_path / "own", tmp_path / "other"
    window, tray = places.instance_name(other), places.tray_instance_name()
    engine = Engine(sites=[other, own])
    sockets = Sockets({window: 4242, tray: 4343})
    with caplog.at_level(logging.INFO, logger="folio_exchange"):
        res = run(own, engine, sockets)
    assert res["ok"] and res["stage"] == "swap" and res["version"] == NEW
    (_, tag, kw), = engine.asked("prepare")
    assert tag == f"v{NEW}" and kw["key"] == b"k" * 32
    assert {4242, 4343} <= kw["allow_pids"]                        # the open window and the tray may be running
    assert kw["sites"] == [other, own]                              # each site known is copied
    assert sockets.asked == [window, tray]                          # asked only after the release was ready
    (_, plan, restart), = engine.asked("start_swap")
    assert plan == "plan.json"
    assert restart == [argv(startup.app_command(other)), argv(startup.tray_command()),
                       argv(startup.app_command(own, "options"))]
    said = [r.getMessage() for r in caplog.records]
    assert any(s.startswith(f"Getting FOLIO-Exchange {NEW} ready") for s in said)
    assert "Closed the window for other." in said and "Closed the tray." in said


def test_nothing_is_closed_when_the_engine_refuses(tmp_path):
    other = tmp_path / "other"
    refusal = {"ok": False, "stage": "busy", "program": "old", "done": [], "left": [],
               "reason": "Scheduled runs start between minutes 5 and 15 of each hour. Try the update again after "
                         "that. Nothing was changed."}
    engine = Engine(prepared=refusal, sites=[other])
    sockets = Sockets({places.instance_name(other): 1})
    assert run(tmp_path / "own", engine, sockets) == refusal
    assert sockets.asked == [] and engine.asked("start_swap") == []


def test_a_busy_window_is_waited_for_and_said(tmp_path, caplog):
    own, other = tmp_path / "own", tmp_path / "Lakeside"
    name = places.instance_name(other)
    engine = Engine(sites=[other])
    sockets = Sockets({name: 7}, {name: ["busy", "busy", "quit"]})
    sleeps = []
    with caplog.at_level(logging.INFO, logger="folio_exchange"):
        res = run(own, engine, sockets, sleeps=sleeps)
    assert res["ok"] and len(sleeps) == 2
    said = [r.getMessage() for r in caplog.records]
    assert said.count("Waiting for the window for Lakeside to finish its action. The update goes on after that.") == 1
    assert engine.asked("start_swap")[0][2][0] == argv(startup.app_command(other))


def test_a_window_that_closed_after_saying_busy_is_started_again(tmp_path):
    other = tmp_path / "other"
    name = places.instance_name(other)
    engine = Engine(sites=[other])
    res = run(tmp_path / "own", engine, Sockets({name: 7}, {name: ["busy", "gone"]}))
    assert res["ok"] and engine.asked("start_swap")[0][2][0] == argv(startup.app_command(other))


def test_a_window_busy_too_long_stops_the_update_and_the_closed_ones_come_back(tmp_path):
    own, busy, idle = tmp_path / "own", tmp_path / "Lakeside", tmp_path / "Hillside"
    tray = places.tray_instance_name()
    engine = Engine(sites=[busy, idle])
    sockets = Sockets({places.instance_name(busy): 1, places.instance_name(idle): 2, tray: 3},
                      {places.instance_name(busy): ["busy"]})
    launched = []
    res = run(own, engine, sockets, launched=launched, wait_seconds=12)
    assert res["ok"] is False and res["stage"] == "busy"
    assert res["reason"] == ("The window for Lakeside was still running an action, so the update stopped. Nothing "
                             "was changed. Install it again once that action has finished.")
    assert res["done"][-1] == f"FOLIO-Exchange {updating.__version__} is still installed."
    assert engine.asked("start_swap") == []
    assert launched == [argv(startup.app_command(idle)), argv(startup.tray_command())]


def test_the_swap_waits_out_the_minutes_when_the_scheduled_task_starts(tmp_path, caplog):
    times = iter([CALM.replace(minute=8)] * 3 + [CALM.replace(minute=16)] * 10)
    sleeps = []
    with caplog.at_level(logging.INFO, logger="folio_exchange"):
        res = run(tmp_path / "own", Engine(), now=lambda: next(times), sleeps=sleeps)
    assert res["ok"] and len(sleeps) == 3
    assert ("Waiting for minute 16: scheduled runs start between minutes 5 and 15 of each hour."
            in [r.getMessage() for r in caplog.records])


def test_a_swap_that_cannot_start_starts_the_closed_ones_again(tmp_path):
    other = tmp_path / "other"
    failed = {"ok": False, "stage": "swap", "program": "old", "done": [], "left": [],
              "reason": "The update couldn't start (Access is denied). Nothing was changed."}
    launched = []
    res = run(tmp_path / "own", Engine(started=failed, sites=[other]), Sockets({places.instance_name(other): 1}),
              launched=launched)
    assert res["ok"] is False and res["reason"] == failed["reason"]
    assert launched == [argv(startup.app_command(other))]


def test_the_real_engine_gets_the_release_ready_from_the_tag_and_key(install, key, tmp_path, caplog):  # noqa: F811
    """The real prepare and start_swap against PR 36's temporary install folder: progress lines in order."""
    updates_dir = tmp_path / "app" / "updates"
    started = []

    class Real:
        known_sites = staticmethod(lambda: [])
        build_environment = Builder()

        @staticmethod
        def prepare(tag, **kw):
            return selfupdate.prepare(tag, install=install, current=OLD, run=Runner(), now=CALM,
                                      processes=lambda folder: [], updates_dir=updates_dir,
                                      protect=lambda *a, **k: None, **kw)

        @staticmethod
        def start_swap(plan, restart=()):
            started.append((json.loads(Path(plan).read_text(encoding="utf-8")), list(restart)))
            return {"ok": True, "stage": "swap", "program": "old", "done": [], "left": [], "reason": None}

    site = make_site(tmp_path / "sites" / "lakeside", "https://folio.example.org")
    with caplog.at_level(logging.INFO, logger="folio_exchange"):
        res = updating.install(NEW, key=key[1], own=site, engine=Real(), probe=lambda n: None,
                               ask_quit=pytest.fail, launch=pytest.fail, sleep=pytest.fail, now=lambda: CALM,
                               http=FakeGitHub(release(key[0])))
    assert res["ok"], res
    plan, restart = started[0]
    assert plan["version"] == NEW and restart == [argv(startup.app_command(site, "options"))]
    said = [r.getMessage() for r in caplog.records if r.name == "folio_exchange.desktop.updating"]
    assert said[:6] == [f"Getting FOLIO-Exchange {NEW} ready. The version you have keeps running meanwhile.",
                        "Downloading the signed checksums…",
                        f"The signature checks out. Downloading {selfupdate.zip_name(NEW)}…",
                        f"Copying the site {site}…",
                        "Building the new program folder. This takes a few minutes…",
                        f"FOLIO-Exchange {NEW} is ready."]


def test_a_download_with_another_keys_signature_stops_before_anything_closes(install, key, tmp_path):  # noqa: F811
    other, _public = make_key()

    class Real(Engine):
        def prepare(self, tag, **kw):
            return selfupdate.prepare(tag, install=install, current=OLD, run=Runner(), now=CALM,
                                      processes=lambda folder: [], updates_dir=tmp_path / "updates",
                                      protect=lambda *a, **k: None, **kw)

    engine = Real()
    res = updating.install(NEW, key=key[1], own=None, engine=engine, probe=lambda n: None, ask_quit=pytest.fail,
                           sleep=pytest.fail, now=lambda: CALM, http=FakeGitHub(release(other)))
    assert res["ok"] is False and res["stage"] == "verify"
    assert res["reason"].startswith("The download isn't signed with FOLIO-Exchange's release key.")
    assert engine.asked("start_swap") == []


# ------------------------------------------------------------------ afterwards
def write_result(folder: Path, **result) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / selfupdate.RESULT).write_text(json.dumps(result), encoding="utf-8")


NOW = datetime(2026, 11, 16, 11, 0, tzinfo=timezone.utc)


def test_updated_to_is_said_for_a_day_on_the_new_version(tmp_path):
    write_result(tmp_path, ok=True, version=NEW, **{"from": OLD}, finished=(NOW - timedelta(minutes=2)).isoformat())
    assert updating.last_update(updates_dir=tmp_path, now=NOW, running=NEW) == f"Updated to {NEW}."
    assert updating.last_update(updates_dir=tmp_path, now=NOW + timedelta(days=1), running=NEW) is None
    assert updating.last_update(updates_dir=tmp_path, now=NOW, running=OLD) is None    # another version runs


def test_an_update_that_did_not_finish_says_so_and_why(tmp_path):
    reason = "FOLIO-Exchange is still running from C:\\FX\\program. Nothing was changed. Close it and try again."
    write_result(tmp_path, ok=False, version=NEW, **{"from": OLD}, reason=reason, finished=NOW.isoformat())
    assert updating.last_update(updates_dir=tmp_path, now=NOW, running=OLD) == (
        f"The update to {NEW} didn't finish. FOLIO-Exchange {OLD} is still installed. {reason}")


def test_no_result_says_nothing(tmp_path):
    assert updating.last_update(updates_dir=tmp_path, now=NOW) is None


def test_the_swap_writes_what_the_next_start_says(install, key, tmp_path):  # noqa: F811
    """The real swap, then the line: the result file is the one handover between the versions."""
    updates_dir = tmp_path / "app" / "updates"
    ready = selfupdate.prepare(f"v{NEW}", key=key[1], install=install, current=OLD, http=FakeGitHub(release(key[0])),
                               builder=Builder(), run=Runner(), sites=(), now=CALM, processes=lambda f: [],
                               updates_dir=updates_dir, protect=lambda *a, **k: None)
    selfupdate.swap(ready["plan"], run=Runner(), processes=lambda f: [], now=lambda: CALM, sleep=lambda s: None)
    assert updating.last_update(updates_dir=updates_dir, running=NEW) == f"Updated to {NEW}."


def test_the_update_is_recorded_once_in_each_sites_audit_log(tmp_path):
    a = make_site(tmp_path / "a", "https://folio.example.org")
    b = make_site(tmp_path / "b", "https://folio.example.org")
    gone = tmp_path / "gone"
    write_result(tmp_path / "updates", ok=True, version=NEW, **{"from": OLD}, finished=NOW.isoformat())
    assert updating.record_update(updates_dir=tmp_path / "updates", sites=[a, gone, b]) == [str(a), str(b)]
    assert updating.record_update(updates_dir=tmp_path / "updates", sites=[a, b]) == []          # once
    for root in (a, b):
        with History(load_site(root).history_path) as hist:
            entries = [e for e in hist.audit_log() if e["action"] == "program updated"]
        assert len(entries) == 1 and entries[0]["detail"] == {"from": OLD, "to": NEW, "via": "app"}


def test_an_update_that_did_not_finish_is_not_recorded(tmp_path):
    a = make_site(tmp_path / "a", "https://folio.example.org")
    write_result(tmp_path / "updates", ok=False, version=NEW, **{"from": OLD}, finished=NOW.isoformat())
    assert updating.record_update(updates_dir=tmp_path / "updates", sites=[a]) == []
