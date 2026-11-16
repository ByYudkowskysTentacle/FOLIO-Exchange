"""The last step of an in-place update: swap the program folder for the new one built beside it.

Windows won't rename a folder while a program runs from it, so this step can't run from the program folder.
``selfupdate.start_swap`` copies this file out of it and runs it with the Python the install was made from
(``-I``, so nothing from the program folder or the current folder is imported). For that reason it uses the
standard library only: no FOLIO-Exchange module, no requests, no Qt. A test checks the imports.

What it does, given the plan ``selfupdate.prepare`` wrote:

1. waits up to two minutes for every FOLIO-Exchange program in the program folder to quit, and out of
   minutes 5 to 15 of the hour, when the scheduled task starts;
2. renames ``program`` to ``program.outgoing`` and the new ``program.new`` to ``program``, so the scheduled
   task's program path stays the same;
3. reinstalls FOLIO-Exchange's own wheel in the new folder without downloading anything, so its commands
   (``folio-exchange.exe``, ``folio-exchange-app.exe``) name the folder's new path, and checks that
   ``folio-exchange --version`` reports the new version;
4. on success keeps the folder it replaced as ``program.previous`` (removing the one before it), so going back
   is possible; on any failure puts the old folder back as ``program``;
5. removes the ``updating.json`` marker in every case and writes what happened to the plan's result file.

Every outcome is plain data: ``ok``, ``program`` ("new", "old" or "none"), ``done``, ``left`` and ``reason``.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PLAN_FORMAT = 1
PROGRAM = "program"
NEW = "program.new"
OUTGOING = "program.outgoing"
PREVIOUS = "program.previous"
FAILED = "program.failed"
MARKER = "updating.json"
QUIET = (5, 15)            # minutes of the hour when the scheduled task starts, inclusive
WAIT_SECONDS = 120         # for the windows and the tray to quit
STEP_SECONDS = 2
COMMAND_SECONDS = 600
_VERSION = re.compile(r"\b(\d+\.\d+\.\d+)\b")


# ------------------------------------------------------------------ the layout of a program folder
def scripts_dir(env: Path) -> Path:
    return Path(env) / ("Scripts" if os.name == "nt" else "bin")


def env_python(env: Path) -> Path:
    return scripts_dir(env) / ("python.exe" if os.name == "nt" else "python")


def program_exe(env: Path) -> Path:
    return scripts_dir(env) / ("folio-exchange.exe" if os.name == "nt" else "folio-exchange")


def reported_version(text: str | None) -> str | None:
    """The first X.Y.Z in what ``--version`` printed (``folio-exchange 0.3.0 (commit …)``)."""
    m = _VERSION.search(text or "")
    return m.group(1) if m else None


def in_quiet_minutes(now: datetime) -> bool:
    return QUIET[0] <= now.minute <= QUIET[1]


# ------------------------------------------------------------------ programs running from a folder
def _inside(path: str, folder: Path) -> bool:
    prefix = os.path.normcase(os.path.abspath(str(folder))).rstrip("\\/") + os.sep
    return os.path.normcase(os.path.abspath(path)).startswith(prefix)


def _windows_processes() -> list[tuple[int, str]]:
    import ctypes
    from ctypes import wintypes

    psapi, kernel32 = ctypes.WinDLL("psapi"), ctypes.WinDLL("kernel32")
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    size = 4096
    while True:
        pids = (wintypes.DWORD * size)()
        used = wintypes.DWORD()
        if not psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(used)):
            return []
        count = used.value // ctypes.sizeof(wintypes.DWORD)
        if count < size:
            break
        size *= 2
    out = []
    for pid in pids[:count]:
        handle = kernel32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            continue
        try:
            buf = ctypes.create_unicode_buffer(32768)
            n = wintypes.DWORD(len(buf))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(n)):
                out.append((int(pid), buf.value))
        finally:
            kernel32.CloseHandle(handle)
    return out


def _proc_processes() -> list[tuple[int, str]]:
    out = []
    for entry in Path("/proc").glob("[0-9]*"):
        try:
            out.append((int(entry.name), os.readlink(entry / "exe")))
        except (OSError, ValueError):
            continue
    return out


def processes_in(folder: Path) -> list[tuple[int, str]]:
    """The processes this user can see whose program file is inside ``folder``: as ``install.ps1`` finds them,
    the page, the app, the tray, a scheduled run or the command line. (On Windows a program folder's
    ``python.exe`` and ``folio-exchange.exe`` start the base Python but stay running themselves, so they count.)"""
    try:
        found = _windows_processes() if os.name == "nt" else _proc_processes()
    except (OSError, AttributeError):
        found = []
    return [(pid, path) for pid, path in found if path and _inside(path, folder)]


# ------------------------------------------------------------------ the swap
def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def _run_quiet(run, argv: list, cwd: Path | None = None):
    return run([str(a) for a in argv], capture_output=True, text=True, encoding="utf-8", errors="replace",
               stdin=subprocess.DEVNULL, timeout=COMMAND_SECONDS, cwd=str(cwd) if cwd else None,
               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _last_line(r) -> str:
    lines = [ln.strip() for ln in f"{getattr(r, 'stdout', '') or ''}\n{getattr(r, 'stderr', '') or ''}".splitlines()
             if ln.strip()]
    return lines[-1] if lines else f"exit code {getattr(r, 'returncode', '?')}"


def version_problem(run, env: Path, expected: str) -> str | None:
    """Why ``env``'s ``folio-exchange --version`` doesn't start or doesn't report ``expected``, or None."""
    try:
        r = _run_quiet(run, [program_exe(env), "--version"], cwd=Path(env).parent)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"it didn't start ({exc})"
    if r.returncode != 0:
        return f"it didn't start ({_last_line(r)})"
    got = reported_version(r.stdout)
    if got != expected:
        return f"it reported version {got or 'unknown'} instead of {expected}"
    return None


def _relink_problem(run, env: Path, wheel: str | None) -> str | None:
    """Reinstall FOLIO-Exchange's own wheel without downloading anything, so its commands name the folder's
    new path (pip writes a program's full path into each command it makes)."""
    if not wheel:
        return None
    try:
        r = _run_quiet(run, [env_python(env), "-I", "-X", "utf8", "-m", "pip", "install", "--no-deps",
                             "--force-reinstall", "--no-index", "--no-input", "--disable-pip-version-check", wheel],
                       cwd=Path(env).parent)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"its commands couldn't be set up ({exc})"
    if r.returncode != 0:
        return f"its commands couldn't be set up ({_last_line(r)})"
    return None


def _remove(path: Path) -> bool:
    if not path.exists():
        return True
    shutil.rmtree(path, ignore_errors=True)
    return not path.exists()


def swap(plan: dict, *, run=subprocess.run, rename=os.rename, processes=processes_in, now=_now,
         sleep=time.sleep, wait_seconds: int = WAIT_SECONDS) -> dict:
    """Swap the program folder for the one the plan built, or leave (or put back) the old one. Never raises
    for an expected failure: the result says what happened."""
    root = Path(plan["root"])
    program, new = root / PROGRAM, Path(plan["new"])
    outgoing, previous, failed = root / OUTGOING, root / PREVIOUS, root / FAILED
    version, old_version = str(plan["version"]), str(plan.get("from") or "")
    done: list[str] = []
    left: list[str] = []
    result = {"ok": False, "version": version, "from": old_version, "program": "old", "done": done,
              "left": left, "reason": None}

    def stop(reason: str) -> dict:
        result["reason"] = reason
        if result["program"] == "old":
            done.append(f"FOLIO-Exchange {old_version} is still installed.")
        return result

    try:
        if plan.get("format") != PLAN_FORMAT:
            return stop("The update's plan is from another version of FOLIO-Exchange. Nothing was changed.")
        if not new.is_dir() or not program.is_dir():
            return stop(f"{new if not new.is_dir() else program} isn't there. Nothing was changed.")

        waited = 0
        while True:
            busy = processes(program)
            if in_quiet_minutes(now()):
                busy = busy or [(0, "the scheduled task's minutes")]
            if not busy or waited >= wait_seconds:
                break
            sleep(STEP_SECONDS)
            waited += STEP_SECONDS
        if processes(program):
            return stop(f"FOLIO-Exchange is still running from {program}. Nothing was changed. Close it and "
                        "try the update again.")
        if in_quiet_minutes(now()):
            return stop("Scheduled runs start between minutes 5 and 15 of each hour. Nothing was changed. Try "
                        "the update again after that.")

        if outgoing.exists() and not _remove(outgoing):
            return stop(f"{outgoing} is left from an earlier update and couldn't be removed. Nothing was changed.")
        try:
            rename(program, outgoing)
        except OSError as exc:
            return stop(f"The program folder couldn't be renamed ({exc.strerror or exc}). Nothing was changed. "
                        "A program may still be using it.")
        try:
            rename(new, program)
        except OSError as exc:
            reason = f"The new program folder couldn't be moved into place ({exc.strerror or exc})."
            try:
                rename(outgoing, program)
            except OSError as back:
                result["program"] = "none"
                left.append(f"Rename {outgoing} to {PROGRAM}, or run the installer again.")
                return stop(f"{reason} The old one couldn't be put back either ({back.strerror or back}).")
            return stop(f"{reason} The old version was put back.")

        problem = _relink_problem(run, program, plan.get("wheel")) or version_problem(run, program, version)
        if problem:
            reason = f"FOLIO-Exchange {version} didn't work after the swap, because {problem}."
            try:
                rename(program, failed)
                rename(outgoing, program)
            except OSError as back:
                result["program"] = "none" if not program.exists() else "new"
                left.append(f"Rename {outgoing} to {PROGRAM}, or run the installer again.")
                return stop(f"{reason} The old version couldn't be put back ({back.strerror or back}).")
            _remove(failed)
            return stop(f"{reason} The old version was put back.")

        result["program"] = "new"
        result["ok"] = True
        done.append(f"Updated to {version}.")
        if not _remove(previous):
            left.append(f"{previous} couldn't be removed. Delete it to free the space.")
        else:
            try:
                rename(outgoing, previous)
                done.append(f"FOLIO-Exchange {old_version} is kept in {previous}, so you can go back to it.")
            except OSError:
                _remove(outgoing)
        return result
    finally:
        marker = root / MARKER
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            left.append(f"Delete {marker}. Scheduled runs skip while it's less than an hour old.")


def _start(argv: list[str], program: Path) -> None:
    """Start a program from the program folder again (a window or the tray the update closed)."""
    if not argv or not _inside(argv[0], program):
        return
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, creationflags=flags, cwd=str(program.parent))
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: swaphelper.py PLAN.json", file=sys.stderr)
        return 2
    plan = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    result = swap(plan)
    result["finished"] = _now().isoformat(timespec="seconds")
    out = Path(plan["result"])
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(result, indent=1), encoding="utf-8")
    os.replace(tmp, out)
    program = Path(plan["root"]) / PROGRAM
    for item in plan.get("restart") or []:
        if isinstance(item, list) and all(isinstance(a, str) for a in item):
            _start(item, program)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
