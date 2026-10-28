"""Who else can change FOLIO-Exchange: the site folder, its settings, the installed program and its Python.

Anyone who can change these files can make the next scheduled run do something else, as the person it runs
as, and so read that person's stored passwords. ``problems`` says which folders other Windows users (or, off
Windows, the group or everyone) can write to, and how to fix each one. It never raises and is quick.

On Windows each folder's access list is read through the Windows API and its entries are matched by SID, so
the result does not depend on the language of account names or on the console's code page. Only when the API
cannot be used does it fall back to the ``icacls`` command and match by name.
"""
from __future__ import annotations

import ctypes
import functools
import os
import re
import stat
import subprocess
import sys
import sysconfig
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

# Accounts that may change the files besides the person themselves: SYSTEM, Administrators, CREATOR OWNER and
# OWNER RIGHTS (whoever made a file), and TrustedInstaller.
TRUSTED_SIDS = ("S-1-5-18", "S-1-5-32-544", "S-1-3-0", "S-1-3-4",
                "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464")
APP_PACKAGE_SIDS = "S-1-15-"       # app package identities (ALL APPLICATION PACKAGES and the like), not people
ENGLISH_NAMES = ("NT AUTHORITY\\SYSTEM", "BUILTIN\\Administrators", "CREATOR OWNER", "OWNER RIGHTS",
                 "NT SERVICE\\TrustedInstaller")
# icacls rights that let an account change a file or folder, or add files to it.
WRITE_RIGHTS = {"F", "M", "W", "D", "DC", "WD", "AD", "WA", "WEA", "WDAC", "WO", "GA", "GW", "MA"}
# The same rights as access mask bits: write data or add a file, append or add a folder, write extended
# attributes, delete a child, write attributes, delete, change permissions, take ownership, maximum allowed,
# generic all and generic write.
WRITE_MASK = (0x2 | 0x4 | 0x10 | 0x40 | 0x100 | 0x10000 | 0x40000 | 0x80000 | 0x02000000 | 0x10000000
              | 0x40000000)
TIMEOUT = 10
PROGRAM = "the installed FOLIO-Exchange program"
PYTHON = "the Python that FOLIO-Exchange runs on"


def install_paths() -> list[Path]:
    """The installed program and the Python under it: the virtual environment (or Python) folder, which holds
    Scripts/folio-exchange.exe, the scripts and site-packages folders, the folio_exchange package, and the Python
    the environment is built on with its standard library."""
    paths = [Path(sys.prefix)]
    for key in ("scripts", "purelib"):
        paths += _sysconfig_path(key)
    paths.append(Path(__file__).resolve().parent)
    base = Path(sys.base_prefix)
    return paths + [base, base / "Lib"] + _sysconfig_path("stdlib")


def _sysconfig_path(key: str) -> list[Path]:
    try:
        return [Path(sysconfig.get_paths()[key])]
    except (KeyError, OSError):
        return []


def _install_label(path: Path) -> str:
    """The base Python's own folders are named as such; everything else is the program."""
    base = Path(sys.base_prefix)
    python = {_key(p) for p in [base, base / "Lib", *_sysconfig_path("stdlib")]}
    program = {_key(p) for p in [Path(sys.prefix), *_sysconfig_path("purelib"), Path(__file__).resolve().parent]}
    return PYTHON if _key(path) in python and _key(path) not in program else PROGRAM


def _key(path: Path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def problems(site: Any) -> list[str]:
    """Plain warnings, one per folder or file that others can change; an empty list when all is well."""
    try:
        targets = _site_paths(site) + [(_install_label(p), p) for p in install_paths()]
        targets = [(label, p) for label, p in _unique(targets) if p.exists()]
        targets.sort(key=lambda t: len(t[1].parts))      # a folder before what is in it
        if sys.platform == "win32":
            return _windows(targets)
        return _posix(targets)
    except Exception as exc:          # a warning helper must never stop check
        return [f"Could not check who can change the FOLIO-Exchange folders ({type(exc).__name__})."]


def _site_paths(site: Any) -> list[tuple[str, Path]]:
    root = Path(site.root)
    out = [("the site folder", root), ("the site settings", root / "site.yaml"),
           ("the feed settings folder", Path(site.feeds_dir)), ("the SFTP keys folder", root / "keys")]
    bundled = Path(__file__).resolve().parent
    for d in getattr(site, "recipe_dirs", []):
        d = Path(d)
        if bundled not in d.resolve().parents:
            out.append(("a local recipes folder", d))
    return out


def _unique(targets: list[tuple[str, Path]]) -> list[tuple[str, Path]]:
    seen: set[str] = set()
    out = []
    for label, p in targets:
        key = _key(p)
        if key not in seen:
            seen.add(key)
            out.append((label, p.resolve()))
    return out


def _inside(path: Path, folders: list[Path]) -> bool:
    return any(f in path.parents for f in folders)


# ---------------------------------------------------------------- Windows
def _windows(targets: list[tuple[str, Path]]) -> list[str]:
    me = _current_sid()
    listings: list[list[dict] | None] = [_acl_entries(p) if me else None for _, p in targets]
    by_name = [i for i, entries in enumerate(listings) if entries is None]
    if by_name:                                   # the Windows API could not be used: ask icacls instead
        try:
            with ThreadPoolExecutor(max_workers=min(8, len(by_name))) as pool:
                texts = list(pool.map(lambda i: _icacls(targets[i][1]), by_name))
        except subprocess.TimeoutExpired:
            return [f"Could not check who can change the FOLIO-Exchange folders: the Windows icacls command did "
                    f"not answer within {TIMEOUT} seconds (a slow network drive or a busy computer). Run "
                    "`folio-exchange check` again later."]
        if any(text is None for text in texts):
            return ["Could not check who can change the FOLIO-Exchange folders: the Windows icacls command is "
                    "not available."]
        for i, text in zip(by_name, texts, strict=True):
            listings[i] = parse_icacls(text, targets[i][1])
    trusted_names = _trusted_names() if by_name else set()
    out: list[str] = []
    flagged: list[Path] = []
    for (label, path), listing in zip(targets, listings, strict=True):
        entries = [e for e in listing or [] if not e["deny"] and e["write"]
                   and not _is_trusted(e, me, trusted_names)]
        if not entries:
            continue
        if _inside(path, flagged) and all(e["inherited"] for e in entries):
            continue                  # the fix for the folder above fixes this too
        flagged.append(path)
        out.append(_windows_message(label, path, sorted({_who(e) for e in entries})))
    return out


def _is_trusted(entry: dict, me: str | None, trusted_names: set[str]) -> bool:
    sid = entry.get("sid")
    if sid:
        return sid in TRUSTED_SIDS or sid == me or sid.startswith(APP_PACKAGE_SIDS)
    name = entry["who"].lower()
    if name.startswith("application package authority\\"):
        return True
    if "?" not in name:
        return name in trusted_names
    # Letters outside the console's code page come back as "?": let each stand for any one letter.
    pattern = re.compile(".".join(re.escape(part) for part in name.split("?")), re.DOTALL)
    return any(pattern.fullmatch(t) for t in trusted_names)


def _who(entry: dict) -> str:
    if entry.get("sid"):
        return _name_of_sid(entry["sid"]) or entry["sid"]
    return entry["who"]


def _icacls(path: Path) -> str | None:
    """icacls's listing of a path, or None when it cannot be run. Raises subprocess.TimeoutExpired when it
    does not answer in time."""
    try:
        done = subprocess.run(["icacls", str(path)], capture_output=True, timeout=TIMEOUT,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.decode("oem", errors="replace")


def parse_icacls(text: str, path: Path) -> list[dict]:
    """The access entries in icacls output: who, their rights, whether they allow writing, and whether the
    entry is inherited or a deny."""
    out = []
    first = True
    for raw in text.splitlines():
        if not raw.strip():
            continue
        # The first line starts with the path itself. Cut it by length: letters outside the console's code page
        # come back as "?", so the text may not match.
        line = (raw[len(str(path)):] if first else raw).strip()
        first = False
        m = re.match(r"^(.+?):((?:\([^)]*\))+)$", line)
        if not m:
            continue
        flags = re.findall(r"\(([^)]*)\)", m.group(2))
        rights: set[str] = set()
        for f in flags:
            rights.update(r.strip().upper() for r in f.split(","))
        out.append({"who": m.group(1).strip(), "sid": None, "rights": rights, "write": bool(rights & WRITE_RIGHTS),
                    "inherited": "I" in rights, "deny": "DENY" in rights})
    return out


def _trusted_names() -> set[str]:
    """For the icacls fallback only: the trusted accounts and this user, by name."""
    names = {n.lower() for n in ENGLISH_NAMES}
    for sid in TRUSTED_SIDS:
        name = _name_of_sid(sid)
        if name:
            names.add(name.lower())
    user, domain = os.environ.get("USERNAME", ""), os.environ.get("USERDOMAIN", "")
    if user:
        names.add(f"{domain}\\{user}".lower() if domain else user.lower())
        computer = os.environ.get("COMPUTERNAME", "")
        if computer:
            names.add(f"{computer}\\{user}".lower())
    return names


# The Windows API, through ctypes. Every function returns None when it cannot answer, never raising.
SE_FILE_OBJECT = 1
DACL_SECURITY_INFORMATION = 4
TOKEN_QUERY = 0x8
TOKEN_USER = 1
INHERITED_ACE = 0x10
ALLOW_TYPES, DENY_TYPES = (0x0, 0x9), (0x1, 0xA)     # access allowed and denied, and their callback forms


class _AclHeader(ctypes.Structure):
    _fields_ = [("AclRevision", ctypes.c_ubyte), ("Sbz1", ctypes.c_ubyte), ("AclSize", ctypes.c_ushort),
                ("AceCount", ctypes.c_ushort), ("Sbz2", ctypes.c_ushort)]


class _AceStart(ctypes.Structure):
    """An access allowed or denied entry up to its SID, which follows straight after."""
    _fields_ = [("AceType", ctypes.c_ubyte), ("AceFlags", ctypes.c_ubyte), ("AceSize", ctypes.c_ushort),
                ("Mask", ctypes.c_uint32)]


@functools.cache
def _api() -> tuple[Any, Any] | None:
    """advapi32 and kernel32 with their argument types set, or None off Windows."""
    try:
        from ctypes import wintypes

        advapi = ctypes.WinDLL("advapi32", use_last_error=True)   # type: ignore[attr-defined]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)   # type: ignore[attr-defined]
    except (AttributeError, OSError, ImportError, ValueError):
        return None
    vp, pvp, dword = ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), wintypes.DWORD
    pdword = ctypes.POINTER(dword)
    for fn, args, res in (
        (advapi.GetNamedSecurityInfoW, [wintypes.LPCWSTR, ctypes.c_int, dword, pvp, pvp, pvp, pvp, pvp], dword),
        (advapi.GetAce, [vp, dword, pvp], wintypes.BOOL),
        (advapi.ConvertSidToStringSidW, [vp, pvp], wintypes.BOOL),
        (advapi.ConvertStringSidToSidW, [wintypes.LPCWSTR, pvp], wintypes.BOOL),
        (advapi.LookupAccountSidW, [wintypes.LPCWSTR, vp, wintypes.LPWSTR, pdword, wintypes.LPWSTR, pdword,
                                    pdword], wintypes.BOOL),
        (advapi.OpenProcessToken, [wintypes.HANDLE, dword, ctypes.POINTER(wintypes.HANDLE)], wintypes.BOOL),
        (advapi.GetTokenInformation, [wintypes.HANDLE, ctypes.c_int, vp, dword, pdword], wintypes.BOOL),
        (kernel.GetCurrentProcess, [], wintypes.HANDLE),
        (kernel.CloseHandle, [wintypes.HANDLE], wintypes.BOOL),
        (kernel.LocalFree, [vp], vp),
    ):
        fn.argtypes, fn.restype = args, res
    return advapi, kernel


def _sid_text(psid: int) -> str | None:
    api = _api()
    if api is None or not psid:
        return None
    advapi, kernel = api
    out = ctypes.c_void_p()
    if not advapi.ConvertSidToStringSidW(psid, ctypes.byref(out)):
        return None
    try:
        return ctypes.wstring_at(out.value)
    finally:
        kernel.LocalFree(out)


@functools.cache
def _current_sid() -> str | None:
    """This Windows user's SID, looked up once."""
    api = _api()
    if api is None:
        return None
    try:
        from ctypes import wintypes

        advapi, kernel = api
        token = wintypes.HANDLE()
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            buf, size = ctypes.create_string_buffer(1024), wintypes.DWORD()
            if not advapi.GetTokenInformation(token, TOKEN_USER, buf, len(buf), ctypes.byref(size)):
                return None
            return _sid_text(ctypes.c_void_p.from_buffer(buf).value or 0)   # TOKEN_USER starts with the SID
        finally:
            kernel.CloseHandle(token)
    except (OSError, ValueError, ctypes.ArgumentError):
        return None


def _acl_entries(path: Path) -> list[dict] | None:
    """The allow and deny entries of a file's or folder's access list, by SID, or None when it cannot be read."""
    api = _api()
    if api is None:
        return None
    advapi, kernel = api
    try:
        dacl, sd = ctypes.c_void_p(), ctypes.c_void_p()
        if advapi.GetNamedSecurityInfoW(str(path), SE_FILE_OBJECT, DACL_SECURITY_INFORMATION, None, None,
                                        ctypes.byref(dacl), None, ctypes.byref(sd)):
            return None
        try:
            if not dacl.value:            # no access list at all: everyone may do anything
                return [{"who": "Everyone", "sid": "S-1-1-0", "write": True, "inherited": False, "deny": False}]
            out = []
            for i in range(_AclHeader.from_address(dacl.value).AceCount):
                ace = ctypes.c_void_p()
                if not advapi.GetAce(dacl, i, ctypes.byref(ace)) or not ace.value:
                    return None
                head = _AceStart.from_address(ace.value)
                if head.AceType not in ALLOW_TYPES + DENY_TYPES:
                    continue              # object entries belong to Active Directory, not to files
                sid = _sid_text(ace.value + ctypes.sizeof(_AceStart))
                if sid is None:
                    return None
                out.append({"who": sid, "sid": sid, "write": bool(head.Mask & WRITE_MASK),
                            "inherited": bool(head.AceFlags & INHERITED_ACE), "deny": head.AceType in DENY_TYPES})
            return out
        finally:
            kernel.LocalFree(sd)
    except (OSError, ValueError, ctypes.ArgumentError):
        return None


def _name_of_sid(sid: str) -> str | None:
    """The account name for a SID in this computer's language, or None."""
    api = _api()
    if api is None:
        return None
    try:
        from ctypes import wintypes

        advapi, kernel = api
        psid = ctypes.c_void_p()
        if not advapi.ConvertStringSidToSidW(sid, ctypes.byref(psid)):
            return None
        try:
            name, domain = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
            n, d, use = wintypes.DWORD(256), wintypes.DWORD(256), wintypes.DWORD()
            if not advapi.LookupAccountSidW(None, psid, name, ctypes.byref(n), domain, ctypes.byref(d),
                                            ctypes.byref(use)):
                return None
            return f"{domain.value}\\{name.value}" if domain.value else name.value
        finally:
            kernel.LocalFree(psid)
    except (OSError, ValueError, ctypes.ArgumentError):
        return None


def _windows_message(label: str, path: Path, who: list[str]) -> str:
    user = os.environ.get("USERNAME") or "%USERNAME%"
    domain = os.environ.get("USERDOMAIN")
    account = f"*{_current_sid()}" if _current_sid() else (f"{domain}\\{user}" if domain else user)
    inherit = "(OI)(CI)" if path.is_dir() else ""
    grants = " ".join(f'"{a}:{inherit}F"' for a in (account, "*S-1-5-18", "*S-1-5-32-544"))
    return (f"Other Windows users can change {label}, {path} ({', '.join(who)}). Anyone signed in to this "
            "computer could then change what FOLIO-Exchange does and read the passwords it keeps. Best: install "
            "FOLIO-Exchange under %LOCALAPPDATA% (the installer's default), which only you can change. Or, to "
            "keep this folder, limit it to you, SYSTEM and Administrators with this command: "
            f'icacls "{path}" /inheritance:r /grant:r {grants}')


# ---------------------------------------------------------------- elsewhere
def _posix(targets: list[tuple[str, Path]]) -> list[str]:
    out: list[str] = []
    flagged: list[Path] = []
    for label, path in targets:
        mode = path.stat().st_mode
        if not mode & (stat.S_IWGRP | stat.S_IWOTH):
            continue
        if _inside(path, flagged):
            continue
        flagged.append(path)
        who = "everyone on this computer" if mode & stat.S_IWOTH else "other members of its group"
        out.append(f"{label[0].upper()}{label[1:]}, {path}, can be changed by {who}. They could then change what "
                   "FOLIO-Exchange does and read the passwords it keeps. Limit it to its owner with: "
                   f'chmod -R go-w "{path}"')
    return out
