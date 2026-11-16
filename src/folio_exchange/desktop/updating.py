"""Installing an update from the app: the in-place engine (``selfupdate``, docs/updates-inplace.md) wired to the
Updates section under Options. No Qt here; the local sockets come in through ``probe`` and ``ask_quit``.

- ``offer`` says whether Options may show "Install X.Y.Z…", or why not in plain words: a pip or editable
  install, ``FOLIO_EXCHANGE_UPDATES=notify`` or ``off``, or no release key. The key is ``updates.RELEASE_KEY``
  and nothing else: no setting, file or environment variable can supply one. While it is None there is no
  Install button, and ``install`` refuses before it reaches the engine.
- ``install`` runs as the asking window's running action, in the job's thread, so each line it logs shows in
  Options as progress: it notes which windows and tray are open, has the engine get the release ready (download,
  signature, site copies, new program folder), asks each open window and the tray to quit ("quit for update"
  over ``single.py``'s sockets, waiting while a window has an action running), waits out minutes 5 to 15, and
  starts the swap, which starts again the windows (with ``--site``) and the tray it closed, and this window on
  Options. The window then quits.
- ``last_update`` is the line Options shows for a day after a swap ("Updated to X.Y.Z.", or that it didn't finish),
  and ``record_update`` writes "program updated" with both versions in each site's audit log, once.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .. import __version__, selfupdate, swaphelper
from ..config import load_site
from ..errors import LockBusy
from ..history import History
from ..lock import RunLock
from . import places, startup

log = logging.getLogger(__name__)

WAIT_SECONDS = 15 * 60          # how long the update waits for a window's action to end
STEP_SECONDS = 5
SAID_FOR = timedelta(days=1)    # how long after a swap Options says how it went
AUDITED = "audited"             # set in the result file once each site's audit log has the entry
ACTION = "program updated"

NO_KEY = "This version of FOLIO-Exchange can't install updates itself."
WHAT_HAPPENS = ("FOLIO-Exchange closes, updates and opens again. Your sites, feeds and passwords stay as they "
                "are.")


def _same(a: Path | str, b: Path | str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


# ------------------------------------------------------------------ whether to offer it
def offer(status: dict, install: dict | None = None, key: Any = None, *, find=selfupdate.find_install) -> dict:
    """What Options offers for ``status`` (updates.status()): {"install": bool, "why": str or None}. ``why``
    says why there's no Install button when a newer version is shown that could otherwise be installed; the
    pip command and the release page stay as U0 shows them. ``key``: updates.RELEASE_KEY (the window passes
    it); ``install``: selfupdate.find_install's answer, found when not given."""
    new = status.get("available")
    if not new or status.get("mode") != "on":
        return {"install": False, "why": None}          # "off" and "notify" say so themselves (options.py)
    install = install if install is not None else find(version=new["version"])
    if not install.get("updatable"):
        reason = str(install.get("reason") or "")
        return {"install": False, "why": reason.split(" Update it with this command:")[0] or None}
    if key is None:
        return {"install": False, "why": NO_KEY}
    return {"install": True, "why": None}


# ------------------------------------------------------------------ installing
class _Telling:
    """The engine's GitHub session, saying which file it starts to download, as progress for Options."""

    def __init__(self, http: Any, version: str) -> None:
        self.http, self.zip = http, selfupdate.zip_name(version)

    def get(self, url: str, *args: Any, **kw: Any) -> Any:
        name = url.rsplit("/", 1)[-1]
        if url.startswith(selfupdate.RELEASES):          # the first request for each file, not its redirects
            if name == selfupdate.SUMS:
                log.info("Downloading the signed checksums…")
            elif name == self.zip:
                log.info("The signature checks out. Downloading %s…", self.zip)
        return self.http.get(url, *args, **kw)


def _copying(sites: list[Path]) -> Iterator[Path]:
    for root in sites:
        log.info("Copying the site %s…", root)
        yield root


def _launch(argv: list[str]) -> None:
    """Start a window or the tray again with the version still installed (an update that stopped after closing
    them)."""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, creationflags=flags, cwd=str(Path(argv[0]).parent))
    except OSError as exc:
        log.warning("Couldn't start %s again: %s", argv[0], exc)


def _stopped(stage: str, reason: str, version: str, done: list[str] | None = None) -> dict:
    return {"ok": False, "stage": stage, "program": "old", "version": version, "from": __version__,
            "done": [*(done or []), f"FOLIO-Exchange {__version__} is still installed."], "left": [],
            "reason": reason}


def install(version: str, *, key: Any, own: Path | None, sites: Iterable[Path] | None = None,
            engine: Any = selfupdate, probe: Callable[[str], int | None] | None = None,
            ask_quit: Callable[[str], str] | None = None, launch: Callable[[list[str]], None] = _launch,
            sleep: Callable[[float], None] = time.sleep, now: Callable[[], datetime] | None = None,
            wait_seconds: float = WAIT_SECONDS, http: Any = None) -> dict:
    """Install ``version`` (X.Y.Z) after the person's yes, from the window of the site ``own``. Returns the
    engine's kind of result (``ok``, ``stage``, ``done``, ``left``, ``reason``): ``stage`` "swap" with ``ok``
    when the swap has started and the window should quit now."""
    if key is None:                                     # never reached from the app (offer), but never past here
        return _stopped("key", NO_KEY + " Nothing was downloaded.", version)
    if probe is None or ask_quit is None:
        from . import single

        probe = probe or single.probe
        ask_quit = ask_quit or single.ask_to_quit
    now = now or (lambda: datetime.now(timezone.utc).astimezone())
    tag = f"v{version}"
    sites = list(engine.known_sites() if sites is None else sites)
    if own is not None and not any(_same(own, s) for s in sites):
        sites.insert(0, Path(own))

    # Which windows and tray are open: they run from the program folder too, and are asked to quit later.
    others: list[tuple[str, Path | None, str]] = [("window", Path(s), places.instance_name(Path(s)))
                                                   for s in sites if own is None or not _same(own, s)]
    others.append(("tray", None, places.tray_instance_name()))
    open_now = [(kind, root, name, pid) for kind, root, name in others if (pid := probe(name)) is not None]
    allow = {os.getpid(), os.getppid(), *(pid for *_rest, pid in open_now)}

    log.info("Getting FOLIO-Exchange %s ready. The version you have keeps running meanwhile.", version)
    session = http if http is not None else _session()

    def build(*args: Any, **kw: Any) -> Any:
        log.info("Building the new program folder. This takes a few minutes…")
        return engine.build_environment(*args, **kw)

    res = engine.prepare(tag, key=key, http=_Telling(session, version), builder=build, sites=_copying(sites),
                         allow_pids=allow)
    if not res.get("ok"):
        return res
    log.info("FOLIO-Exchange %s is ready.", version)

    closed: list[tuple[str, Path | None]] = []

    def give_back() -> None:
        for kind, root in closed:
            exe, args, _folder = startup.tray_command() if kind == "tray" else startup.app_command(root)
            launch([exe, *args])

    def quiet_minutes() -> None:
        said = False
        while swaphelper.in_quiet_minutes(now()):
            if not said:
                log.info("Waiting for minute %d: scheduled runs start between minutes %d and %d of each hour.",
                         swaphelper.QUIET[1] + 1, *swaphelper.QUIET)
                said = True
            sleep(STEP_SECONDS)

    quiet_minutes()
    pending = [(kind, root, name) for kind, root, name, _pid in open_now]
    waited, waiting_for = 0.0, None
    while pending:
        busy = []
        for kind, root, name in pending:
            answer = ask_quit(name)
            if answer == "busy":
                busy.append((kind, root, name))
            elif answer == "quit" or (answer == "gone" and waiting_for is not None):
                closed.append((kind, root))             # gone after saying busy: it closed meanwhile
                log.info("Closed %s.", "the tray" if kind == "tray" else f"the window for {root.name}")
        pending = busy
        if not pending:
            break
        names = ", ".join(root.name for _kind, root, _name in pending if root is not None)
        if waited >= wait_seconds:
            give_back()
            return _stopped("busy", f"The window for {names} was still running an action, so the update stopped. "
                                    "Nothing was changed. Install it again once that action has finished.",
                            version, res.get("done"))
        if names != waiting_for:
            log.info("Waiting for the window for %s to finish its action. The update goes on after that.", names)
            waiting_for = names
        sleep(STEP_SECONDS)
        waited += STEP_SECONDS
    quiet_minutes()

    restart: list[list[str]] = []
    for kind, root in closed:
        exe, args, _folder = startup.tray_command() if kind == "tray" else startup.app_command(root)
        restart.append([exe, *args])
    if own is not None:
        exe, args, _folder = startup.app_command(Path(own), "options")
        restart.append([exe, *args])
    log.info("Closing FOLIO-Exchange to switch to %s…", version)
    started = engine.start_swap(res["plan"], restart=restart)
    if not started.get("ok"):
        give_back()
        return {**started, "version": version, "from": __version__}
    return {**started, "version": version, "from": __version__, "done": res.get("done") or []}


def _session() -> Any:
    import requests

    return requests.Session()


# ------------------------------------------------------------------ afterwards
def _finished(result: dict) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(result.get("finished")))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def last_update(*, updates_dir: Path | None = None, now: datetime | None = None,
                running: str = __version__) -> str | None:
    """What Options says on the next start, for a day after a swap: "Updated to X.Y.Z." when this is that
    version, or that the update didn't finish and which version is still installed, with why."""
    result = selfupdate.last_result(updates_dir)
    if not result:
        return None
    finished = _finished(result)
    age = (now or datetime.now(timezone.utc)) - finished if finished is not None else None
    if age is None or not -timedelta(minutes=5) <= age < SAID_FOR:     # a clock a little behind still counts
        return None
    version, before = str(result.get("version") or ""), str(result.get("from") or "")
    if result.get("ok"):
        same = selfupdate.parse_version(version) is not None and \
            selfupdate.parse_version(version) == selfupdate.parse_version(running)
        return f"Updated to {version}." if same else None
    reason = str(result.get("reason") or "").strip()
    line = f"The update to {version} didn't finish. FOLIO-Exchange {before or running} is still installed."
    return f"{line} {reason}" if reason else line


def _write(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def record_update(*, updates_dir: Path | None = None, sites: Iterable[Path] | None = None) -> list[str]:
    """After a swap that worked, write "program updated" with both versions in the audit log of each site the
    app knows, once: the window and the tray both call this when they start, and the first does it. Returns the
    sites recorded in (empty when there was nothing to do). A site whose history can't be opened is skipped and
    logged."""
    folder = Path(updates_dir) if updates_dir is not None else selfupdate.updates_folder()
    result = selfupdate.last_result(folder)
    if not result or not result.get("ok") or result.get(AUDITED):
        return []
    try:
        with RunLock(folder / "audit.lock"):
            result = selfupdate.last_result(folder) or {}
            if not result.get("ok") or result.get(AUDITED):
                return []
            done = []
            for root in (selfupdate.known_sites() if sites is None else sites):
                try:
                    with History(load_site(Path(root)).history_path) as hist:
                        hist.audit(None, None, ACTION, {"from": result.get("from"), "to": result.get("version"),
                                                        "via": "app"})
                    done.append(str(root))
                except Exception as exc:  # noqa: BLE001 - one site that can't be opened doesn't stop the others
                    log.warning("Couldn't record the update in %s: %s", root, exc)
            _write(folder / selfupdate.RESULT, {**result, AUDITED: True})
            log.info("Recorded the update to %s in %d site(s)", result.get("version"), len(done))
            return done
    except LockBusy:
        return []                                       # the tray or another window is recording it
