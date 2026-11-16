"""Installing an update from the app: the in-place engine (``selfupdate``, docs/updates-inplace.md) wired to the
Updates section under Options. No Qt here; the local sockets come in through ``probe`` and ``ask_quit``.

- ``offer`` says whether Options may show "Install X.Y.Z…", or why not in plain words: a pip or editable
  install, ``FOLIO_EXCHANGE_UPDATES=notify`` or ``off``, or no release key. The key is ``updates.RELEASE_KEY``
  and nothing else: no setting, file or environment variable can supply one. While it is None there is no
  Install button, and ``install`` refuses before it reaches the engine.
- ``install`` runs as the asking window's running action, in the job's thread, so each line it logs shows in
  Options as progress: it has the engine get the release ready (download, signature, site copies, new program
  folder), then looks again for the open windows and the tray (one opened during the build counts too), asks each
  whether it could quit ("may-quit" over ``single.py``'s sockets) and waits while one has an action running or a
  form open, closes them all only once none is busy ("quit-for-update"), waits out minutes 5 to 15, and starts the
  swap, which starts again the windows (with ``--site``) and the tray it closed, and this window on Options. The
  window then quits.
- ``last_update`` is the line Options shows for a day after a swap ("Updated to X.Y.Z.", or that it didn't finish),
  and ``record_update`` writes "program updated" with both versions in each site's audit log, once.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import time
from collections.abc import Callable, Iterable
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
ANCESTORS = 3                   # how far up from each open window, the tray and this window their launchers may be
SAID_FOR = timedelta(days=1)    # how long after a swap Options says how it went
AUDITED = "audited"             # in the result file: the sites whose audit log has the entry
LOCKED_TRIES = 5                # a site's history that is busy (another connection writing) is tried this often
LOCKED_WAIT = 0.5               # seconds between those tries
ACTION = "program updated"

NO_KEY = "This version of FOLIO-Exchange can't install updates itself."
WHAT_HAPPENS = ("FOLIO-Exchange closes, updates and opens again, and so do its other windows and the tray icon. It "
                "takes a few minutes and needs the internet. Your sites, feeds and passwords stay as they are.")
STARTED = "started.json"        # in the updates folder: the swap this window started, for a helper that never ended
ROUNDS = 10                     # looks for windows opened while the others were being closed


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
            ask_may: Callable[[str], str] | None = None, ask_quit: Callable[[str], str] | None = None,
            parents: Callable[[], dict[int, int]] | None = None, launch: Callable[[list[str]], None] = _launch,
            sleep: Callable[[float], None] = time.sleep, now: Callable[[], datetime] | None = None,
            wait_seconds: float = WAIT_SECONDS, http: Any = None) -> dict:
    """Install ``version`` (X.Y.Z) after the person's yes, from the window of the site ``own``. Returns the
    engine's kind of result (``ok``, ``stage``, ``done``, ``left``, ``reason``): ``stage`` "swap" with ``ok``
    when the swap has started and the window should quit now."""
    if key is None:                                     # never reached from the app (offer), but never past here
        return _stopped("key", NO_KEY + " Nothing was downloaded.", version)
    if probe is None or ask_may is None or ask_quit is None:
        from . import single

        probe = probe or single.probe
        ask_may = ask_may or single.ask_may_quit
        ask_quit = ask_quit or single.ask_to_quit
    now = now or (lambda: datetime.now(timezone.utc).astimezone())
    tag = f"v{version}"

    def known() -> list[Path]:
        found = [Path(s) for s in (engine.known_sites() if sites is None else sites)]
        if own is not None and not any(_same(own, s) for s in found):
            found.insert(0, Path(own))
        return found

    def open_ones() -> list[tuple[str, Path | None, str, int]]:
        """The windows (one per known site, but this one) and the tray open now, with their process ids."""
        names = [("window", s, places.instance_name(s)) for s in known() if own is None or not _same(own, s)]
        names.append(("tray", None, places.tray_instance_name()))
        return [(kind, root, name, pid) for kind, root, name in names if (pid := probe(name)) is not None]

    # The ones open now, and their ancestors and this window's, may run while the release is prepared: on Windows
    # the program folder's folio-exchange-app.exe starts the venv's pythonw.exe, which starts the base Python that
    # answers on the socket, and both stay running from the program folder (as swaphelper.own_chain finds for this
    # process). An ancestor outside it (Explorer, say) is never counted anyway.
    tree = (parents or swaphelper.parent_pids)()

    def lineage(pid: int) -> list[int]:
        out: list[int] = []
        for _step in range(ANCESTORS):
            pid = tree.get(pid) or 0
            if pid <= 0 or pid in out:
                break
            out.append(pid)
        return out

    allow = {os.getpid(), os.getppid(), *lineage(os.getpid())}
    for *_rest, pid in open_ones():
        allow |= {pid, *lineage(pid)}

    log.info("Getting FOLIO-Exchange %s ready. The version you have keeps running meanwhile.", version)
    session = http if http is not None else engine.download_session()     # no .netrc account, no default headers

    def build(*args: Any, **kw: Any) -> Any:
        log.info("Building the new program folder. This takes a few minutes…")
        return engine.build_environment(*args, **kw)

    res = engine.prepare(tag, key=key, http=_Telling(session, version), builder=build, sites=known(),
                         allow_pids=allow)
    if not res.get("ok"):
        return res
    log.info("FOLIO-Exchange %s is ready.", version)

    closed: list[tuple[str, Path | None]] = []

    def what(kind: str, root: Path | None) -> str:
        return "the tray" if kind == "tray" else f"the window for {root.name}"

    def give_back() -> None:
        for kind, root in closed:
            exe, args, _folder = startup.tray_command() if kind == "tray" else startup.app_command(root)
            launch([exe, *args])

    def quiet_minutes() -> None:
        said = False
        while swaphelper.in_quiet_minutes(now()):
            if not said:
                log.info("Waiting for minute %d. Scheduled runs start between minutes %d and %d of each hour.",
                         swaphelper.QUIET[1] + 1, *swaphelper.QUIET)
                said = True
            sleep(STEP_SECONDS)

    # Look again now, after the build: a window opened meanwhile must close too, or the swap finds it running and
    # stops after everything else has closed. Ask every one first, and close them only once none is busy, so the
    # idle ones (the tray among them, whose alerts stop while it's closed) don't wait closed for a busy one.
    waited, waiting_for, rounds = 0.0, None, 0
    while True:
        quiet_minutes()
        found = open_ones()
        if not found:
            break
        busy = [(kind, root) for kind, root, name, _pid in found if ask_may(name) == "busy"]
        if busy:
            names = ", ".join(what(kind, root) for kind, root in busy)
            if waited >= wait_seconds:
                give_back()
                return _stopped("busy", f"{names[0].upper()}{names[1:]} still had an action running or a form open, "
                                        "so the update stopped. Nothing was changed. Install it again once that's "
                                        "done.", version, res.get("done"))
            if names != waiting_for:
                log.info("Waiting for %s, which has an action running or a form open. The update goes on once "
                         "it's done.", names)
                waiting_for = names
            sleep(STEP_SECONDS)
            waited += STEP_SECONDS
            continue
        for kind, root, name, _pid in found:
            if ask_quit(name) == "quit":
                closed.append((kind, root))
                log.info("Closed %s.", what(kind, root))
        rounds += 1
        if rounds >= ROUNDS:                            # windows that keep opening: the helper waits for them
            break
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
    plan = Path(res["plan"])                            # updates/X.Y.Z/plan.json
    if plan.is_absolute():                              # so the next start knows, should the helper never end
        try:
            _write(plan.parent.parent / STARTED,
                   {"version": version, "from": __version__, "started": now().isoformat(timespec="seconds")})
        except OSError as exc:
            log.warning("Couldn't note that the update started: %s", exc)
    return {**started, "version": version, "from": __version__, "done": res.get("done") or []}


# ------------------------------------------------------------------ afterwards
def _finished(result: dict) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(result.get("finished")))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _recent(t: datetime | None, now: datetime) -> bool:
    return t is not None and -timedelta(minutes=5) <= now - t < SAID_FOR      # a clock a little behind counts


def last_update(*, updates_dir: Path | None = None, now: datetime | None = None,
                running: str = __version__) -> str | None:
    """What Options says on the next start, for a day after a swap: "Updated to X.Y.Z." when this is that
    version; or that the update didn't finish, which version is installed now (the result's ``program``), why, and
    what's left to do; or, when the helper started but never wrote a result, that it didn't finish."""
    folder = Path(updates_dir) if updates_dir is not None else selfupdate.updates_folder()
    now = now or datetime.now(timezone.utc)
    result = selfupdate.last_result(folder)
    started = _read(folder / STARTED)
    began = _finished({"finished": (started or {}).get("started")})
    finished = _finished(result) if result else None
    if started and _recent(began, now) and (finished is None or finished < began - timedelta(seconds=1)):
        return (f"The update to {started.get('version')} didn't finish. FOLIO-Exchange {running} is installed. "
                "Install it again from here.")
    if not result or not _recent(finished, now):
        return None
    version, before = str(result.get("version") or ""), str(result.get("from") or "")
    left = [str(x) for x in result.get("left") or []]
    if result.get("ok"):
        same = selfupdate.parse_version(version) is not None and \
            selfupdate.parse_version(version) == selfupdate.parse_version(running)
        return " ".join([f"Updated to {version}.", *left]) if same else None
    reason = str(result.get("reason") or "").strip()
    if result.get("program", "old") == "old":
        state = [f"FOLIO-Exchange {before or running} is still installed."]
    else:                                               # a rollback that didn't work: the helper says what's where
        state = [str(x) for x in result.get("done") or [] if str(x).startswith(("FOLIO-Exchange", "No version"))]
    return " ".join([f"The update to {version} didn't finish.", *state, *([reason] if reason else []), *left])


def pending() -> tuple[Path, list[Path]] | None:
    """For the window and the tray, on their own thread before they hand the rest to another: the updates folder
    and the sites known now, or None when no swap has left a result (or a note that one started) on this PC. Both
    are small local files, read here so the thread that records the update never looks up the app's folders
    itself."""
    folder = selfupdate.updates_folder()
    if not (folder / selfupdate.RESULT).is_file() and not (folder / STARTED).is_file():
        return None
    return folder, selfupdate.known_sites()


def _write(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _audit(root: Path, result: dict, sleep: Callable[[float], None]) -> None:
    """One site's entry. Its history may be busy for a moment (the window reading the site as it opens, a run
    writing): a locked database is tried again a few times before it counts as a failure."""
    for attempt in range(LOCKED_TRIES):
        try:
            with History(load_site(root).history_path) as hist:
                hist.audit(None, None, ACTION, {"from": result.get("from"), "to": result.get("version"),
                                                "via": "app"})
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == LOCKED_TRIES - 1:
                raise
            sleep(LOCKED_WAIT)


def record_update(*, updates_dir: Path | None = None, sites: Iterable[Path] | None = None,
                  now: datetime | None = None, sleep: Callable[[float], None] = time.sleep) -> list[str]:
    """After a swap that worked, write "program updated" with both versions in the audit log of each site the
    app knows, once per site: the window and the tray both call this when they start. ``audited`` in the result
    file lists the sites done, so a site that couldn't be written (its history busy, its drive offline) is tried
    again at the next start, for a day after the swap (``SAID_FOR``), and a site added after that never gets an
    entry for an update it didn't go through. Returns the sites recorded in this time."""
    folder = Path(updates_dir) if updates_dir is not None else selfupdate.updates_folder()

    def due(result: dict) -> bool:
        finished = _finished(result)
        age = (now or datetime.now(timezone.utc)) - finished if finished is not None else None
        return bool(result.get("ok")) and age is not None and -timedelta(minutes=5) <= age < SAID_FOR

    result = selfupdate.last_result(folder)
    if not result or not due(result):
        return []
    try:
        with RunLock(folder / "audit.lock"):
            result = selfupdate.last_result(folder) or {}
            if not due(result):
                return []
            audited = [str(s) for s in result.get(AUDITED) or [] if isinstance(s, str)]
            done = []
            for root in (selfupdate.known_sites() if sites is None else sites):
                if any(_same(root, a) for a in audited):
                    continue
                try:
                    _audit(Path(root), result, sleep)
                    done.append(str(root))
                except Exception as exc:  # noqa: BLE001 - one site that can't be written doesn't stop the others
                    log.warning("Couldn't record the update in %s (tried again at the next start): %s", root, exc)
            if done:
                _write(folder / selfupdate.RESULT, {**result, AUDITED: [*audited, *done]})
                log.info("Recorded the update to %s in %d site(s)", result.get("version"), len(done))
            return done
    except LockBusy:
        return []                                       # the tray or another window is recording it
