"""The last step of an in-place update: swap the program folder for the new one built beside it.

It runs from outside the program folder: ``selfupdate.start_swap`` copies this file out of it and runs it with
the Python the install was made from (``-I``, so nothing from the program folder or the current folder is
imported). For that reason it uses the standard library only: no FOLIO-Exchange module, no requests, no Qt. A
test checks the imports.

Windows does let a folder be renamed while a program runs from it, as long as nothing holds a data file open
in it or has its current folder there. A program that kept running across the swap would load its next
modules from the other version. Three things keep that from happening: every FOLIO-Exchange entry point
refuses to start while the ``updating.json`` marker is new (``updating.py``); the swap waits until no program
runs from the program folder, and stops if the list of programs can't be read; and after the first rename it
checks that nothing runs from the renamed folder, and puts it back if something does.

What it does, given the plan ``selfupdate.prepare`` wrote:

1. waits up to two minutes for every FOLIO-Exchange program in the program folder to quit, and out of
   minutes 5 to 15 of the hour, when the scheduled task starts;
2. renames ``program`` to ``program.outgoing``, checks that no program kept running from it, then renames
   ``program.new`` to ``program``, so the scheduled task's program path stays the same. ``prepare`` already
   pointed the new folder's commands at ``program``, so they work the moment it has that name;
3. checks that ``folio-exchange --version`` reports the new version;
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
SETTLE_SECONDS = 10        # for a program caught by the first rename to quit (it finds the marker and stops)
_VERSION = re.compile(r"\b(\d+\.\d+\.\d+)\b")


# ------------------------------------------------------------------ the layout of a program folder
def scripts_dir(env: Path) -> Path:
    return Path(env) / ("Scripts" if os.name == "nt" else "bin")


def env_python(env: Path) -> Path:
    return scripts_dir(env) / ("python.exe" if os.name == "nt" else "python")


def program_exe(env: Path) -> Path:
    return scripts_dir(env) / ("folio-exchange.exe" if os.name == "nt" else "folio-exchange")


def app_exe_name() -> str:
    return "folio-exchange-app.exe" if os.name == "nt" else "folio-exchange-app"


def reported_version(text: str | None) -> str | None:
    """The first X.Y.Z in what ``--version`` printed (``folio-exchange 0.3.0 (commit …)``)."""
    m = _VERSION.search(text or "")
    return m.group(1) if m else None


def in_quiet_minutes(now: datetime) -> bool:
    return QUIET[0] <= now.minute <= QUIET[1]


# ------------------------------------------------------------------ programs running from a folder
class ProcessListError(OSError):
    """The list of running programs couldn't be read, which must never read as "nothing is running"."""


def _inside(path: str, folder: Path) -> bool:
    prefix = os.path.normcase(os.path.abspath(str(folder))).rstrip("\\/") + os.sep
    return os.path.normcase(os.path.abspath(path)).startswith(prefix)


def _windows_processes() -> list[tuple[int, str]]:
    import ctypes
    from ctypes import wintypes

    psapi, kernel32 = ctypes.WinDLL("psapi", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
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
            raise ProcessListError(f"EnumProcesses failed ({ctypes.get_last_error()})")
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
    if not Path("/proc/self/exe").exists():
        raise ProcessListError("there's no /proc to read")
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
    ``python.exe`` and ``folio-exchange.exe`` start the base Python but stay running themselves, so they count.)
    A process this user can't open (another user's, or Windows' own) is left out, as ``install.ps1`` leaves it
    out. Raises ``ProcessListError`` when the list itself can't be read."""
    try:
        found = _windows_processes() if os.name == "nt" else _proc_processes()
    except ProcessListError:
        raise
    except (OSError, AttributeError) as exc:
        raise ProcessListError(str(exc)) from None
    return [(pid, path) for pid, path in found if path and _inside(path, folder)]


def _windows_parents() -> dict[int, int]:
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)              # TH32CS_SNAPPROCESS
    if not snap or snap == ctypes.c_void_p(-1).value:
        return {}
    out = {}
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return out


def _proc_parents() -> dict[int, int]:
    out = {}
    for entry in Path("/proc").glob("[0-9]*"):
        try:
            stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
            out[int(entry.name)] = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
    return out


def parent_pids() -> dict[int, int]:
    """Each process's parent, by process id (empty when it can't be read)."""
    try:
        return _windows_parents() if os.name == "nt" else _proc_parents()
    except (OSError, AttributeError):
        return {}


def own_chain(folder: Path, *, pid: int | None = None, processes=None, parents=None) -> set[int]:
    """This process and the ancestors that started it from ``folder``. Started as ``folio-exchange-app.exe``,
    the app is that launcher, the environment's ``pythonw.exe`` it starts, and the base Python that one starts:
    none of them is another program to wait for."""
    pid = os.getpid() if pid is None else pid
    try:
        inside = {p for p, _path in (processes or processes_in)(folder)}
    except ProcessListError:
        inside = set()
    tree = (parents or parent_pids)()
    chain = {pid}
    parent = tree.get(pid)
    while parent and parent in inside and parent not in chain:
        chain.add(parent)
        parent = tree.get(parent)
    return chain


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

    def running(folder: Path) -> bool | None:
        """Whether a program runs from ``folder``, or None when that can't be told."""
        try:
            return bool(processes(folder))
        except ProcessListError:
            return None

    def running_after_rename() -> bool | None:
        """Whether a program still runs from the renamed folder. Windows goes on listing a running program
        under the path it started from, so one listed under ``program`` counts too (that folder doesn't exist
        between the two renames, so nothing new can have started there)."""
        before, after = running(program), running(outgoing)
        if before is None or after is None:
            return None
        return before or after

    unlisted = ("Couldn't list the programs running on this computer, so it can't tell whether FOLIO-Exchange "
                "is running. Nothing was changed.")
    try:
        if plan.get("format") != PLAN_FORMAT:
            return stop("The update's plan is from another version of FOLIO-Exchange. Nothing was changed.")
        if not new.is_dir() or not program.is_dir():
            return stop(f"{new if not new.is_dir() else program} isn't there. Nothing was changed.")

        waited = 0
        while True:
            busy = running(program)
            if busy is None or (not busy and not in_quiet_minutes(now())) or waited >= wait_seconds:
                break
            sleep(STEP_SECONDS)
            waited += STEP_SECONDS
        busy = running(program)
        if busy is None:
            return stop(unlisted)
        if busy:
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
        # A program that started between the check and the rename now runs from program.outgoing. It finds the
        # marker and quits; anything still there after a few seconds means the old folder goes back.
        settled = 0
        caught = running_after_rename()
        while caught and settled < SETTLE_SECONDS:
            sleep(STEP_SECONDS)
            settled += STEP_SECONDS
            caught = running_after_rename()
        if caught is None or caught:
            reason = unlisted if caught is None else (
                "A program started from the program folder during the update. Nothing was changed. Try the "
                "update again.")
            try:
                rename(outgoing, program)
            except OSError as back:
                result["program"] = "none"
                left.append(f"Rename {outgoing} to {PROGRAM}, or run the installer again.")
                return stop(f"{reason.replace(' Nothing was changed.', '')} The program folder couldn't be put "
                            f"back ({back.strerror or back}).")
            return stop(reason)
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

        problem = version_problem(run, program, version)
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


def _write_result(out: Path, result: dict) -> None:
    result = {**result, "finished": _now().isoformat(timespec="seconds")}
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(result, indent=1), encoding="utf-8")
    os.replace(tmp, out)


def main(argv: list[str] | None = None) -> int:
    """``swaphelper.py PLAN.json ROOT RESULT.json``: the plan, the install folder (whose marker is removed
    whatever happens, even when the plan can't be read) and where to write what happened."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3:
        print("usage: swaphelper.py PLAN.json INSTALL-FOLDER RESULT.json", file=sys.stderr)
        return 2
    plan_path, root, out = Path(args[0]), Path(args[1]), Path(args[2])
    result = {"ok": False, "version": None, "from": None, "program": "old", "done": [], "left": [],
              "reason": "The update couldn't start. Nothing was changed."}
    plan: dict = {}
    try:
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            if not isinstance(plan, dict) or os.path.normcase(os.path.abspath(str(plan.get("root")))) != \
                    os.path.normcase(os.path.abspath(root)):
                raise ValueError("the plan is for another install folder")
        except (OSError, ValueError) as exc:
            result["reason"] = f"The update's plan couldn't be read ({exc}). Nothing was changed."
            plan = {}
        else:
            result = swap(plan)
    except Exception as exc:  # noqa: BLE001 - whatever happens, the marker goes and a result is written
        result["reason"] = f"The update stopped ({type(exc).__name__}: {exc})."
    finally:
        try:
            (root / MARKER).unlink(missing_ok=True)
        except OSError:
            pass
        try:
            _write_result(out, result)
        except OSError:
            pass
    program = root / PROGRAM
    for item in plan.get("restart") or []:
        if isinstance(item, list) and all(isinstance(a, str) for a in item):
            _start(item, program)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
