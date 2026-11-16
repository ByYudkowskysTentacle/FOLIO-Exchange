"""Update a script-based install in place: the engine, with no Qt and nothing that asks the person anything.

``installer/install.ps1`` puts a Python environment in ``<install folder>\\program`` and the sites beside it.
This module updates that environment to a release after the person's yes, without running any downloaded
``.exe``, so it needs neither the one-file installer (D5) nor Windows code signing (D6). See
``docs/updates-inplace.md`` for the design.

``prepare`` does everything that can happen while FOLIO-Exchange is open:

1. checks that this install can update itself (``find_install``), that a release key is set, that the release
   is newer than this version, and that no run or other FOLIO-Exchange program is going;
2. downloads the release's ``SHA256SUMS`` and ``SHA256SUMS.sig`` from GitHub, checks the project's Ed25519
   signature over the first, then downloads the source zip and checks its SHA-256 against it. Redirects are
   followed by hand, to GitHub's hosts only, with a size limit and a stall timeout, into a folder only this
   Windows user can change, under temporary names until every check passes. Any failure deletes the files;
3. copies each site the app knows (settings, feeds, the site's recipes, and its history through SQLite's
   backup API) into ``updates\\from-<version>``. A site that can't be copied stops the update;
4. builds the new environment in ``program.new`` beside the current one and checks that it starts and reports
   the new version, then writes a plan for the swap.

``start_swap`` then copies ``swaphelper.py`` out of the program folder and starts it with the install's base
Python; the app quits, and the helper swaps the folders (see that module). ``swap`` runs the same step in this
process, for tests and the command line.

Each step returns plain data a window can show, as ``siteops.rename_site`` does: ``ok``, ``stage``, ``done``,
``left``, ``reason`` and ``program`` ("old" while the old version is the one installed).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from . import __version__, swaphelper
from .config import BUNDLED_RECIPES, SITE_FILE, load_site
from .errors import FxError, LockBusy
from .lock import RunLock
from .updating import MARKER, marker_active, marker_path, marker_started, update_in_progress, write_marker  # noqa: F401

log = logging.getLogger(__name__)

REPO = "ByYudkowskysTentacle/FOLIO-Exchange"
RELEASES = f"https://github.com/{REPO}/releases"
# github.com redirects a release download to a download host of its own, which it has changed before.
HOSTS = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
SUMS = "SHA256SUMS"
SIG = "SHA256SUMS.sig"
MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_SMALL_BYTES = 64 * 1024
CONNECT_SECONDS = 30
STALL_SECONDS = 60          # no bytes for this long stops a download
MAX_REDIRECTS = 4
UPDATES = "updates"
PLAN = "plan.json"
RESULT = "result.json"
HELPER = "helper"
PART = ".part"

_TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)")
_VERSION_DIR = re.compile(r"^\d+\.\d+\.\d+$")
_SUM_LINE = re.compile(r"^([0-9a-fA-F]{64}) [ *](\S.*)$")

DELETED = "The download didn't pass its checks, so it was deleted. Nothing was installed."


class Stopped(FxError):
    """An update step that stopped, with the stage it stopped at and the sentence to show."""

    def __init__(self, stage: str, reason: str, *, left: list[str] | None = None) -> None:
        super().__init__(reason)
        self.stage = stage
        self.reason = reason
        self.left = left or []


# ------------------------------------------------------------------ versions and names
def zip_name(version: str) -> str:
    """The source zip a release carries as an asset: ``git archive`` of the tag, listed in ``SHA256SUMS``."""
    return f"folio-exchange-{version}.zip"


def tag_version(tag: str) -> tuple[int, int, int] | None:
    m = _TAG.fullmatch(tag or "")
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def parse_version(text: str) -> tuple[int, int, int] | None:
    m = _VERSION.match(text or "")
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def pip_command(version: str) -> str:
    """The command for an install made with pip. Never the bare name, which isn't on PyPI."""
    return (f'pip install "folio-exchange[desktop] @ git+https://github.com/{REPO}.git@v{version}"')


# ------------------------------------------------------------------ the install
def _pyvenv(program: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (program / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip().lower()] = value.strip()
    return out


def _base_python(program: Path) -> Path | None:
    """The Python the program folder was made with, which builds the new one and runs the swap."""
    cfg = _pyvenv(program)
    names = ["python.exe"] if os.name == "nt" else [f"python{sys.version_info[0]}.{sys.version_info[1]}",
                                                     "python3", "python"]
    candidates = [Path(cfg["executable"])] if cfg.get("executable") else []
    candidates += [Path(cfg["home"]) / n for n in names] if cfg.get("home") else []
    return next((c for c in candidates if c.is_file()), None)


def _inside(path: Path, folder: Path) -> bool:
    try:
        Path(os.path.abspath(path)).relative_to(os.path.abspath(folder))
        return True
    except ValueError:
        return False


def find_install(*, prefix: str | os.PathLike | None = None, base_prefix: str | os.PathLike | None = None,
                 package_dir: str | os.PathLike | None = None, version: str | None = None) -> dict:
    """Whether this copy of FOLIO-Exchange is one ``install.ps1`` made, which this engine can update: a Python
    environment called ``program`` with FOLIO-Exchange installed in it (not run from source). Anything else gets
    the command to run instead."""
    prefix = Path(prefix if prefix is not None else sys.prefix)
    base = Path(base_prefix if base_prefix is not None else sys.base_prefix)
    package = Path(package_dir if package_dir is not None else Path(__file__).parent)
    command = pip_command(version or "X.Y.Z")
    out: dict[str, Any] = {"updatable": False, "root": None, "program": None, "python": None, "reason": None,
                           "command": command}
    if os.path.normcase(os.path.abspath(prefix)) == os.path.normcase(os.path.abspath(base)) or \
            prefix.name.lower() != swaphelper.PROGRAM or not (prefix / "pyvenv.cfg").is_file():
        out["reason"] = ("FOLIO-Exchange was installed with pip, so it can't update itself. Update it with this "
                         f"command: {command}")
        return out
    if not _inside(package, prefix):
        out["reason"] = (f"This copy of FOLIO-Exchange runs from {package}, outside its program folder, so it "
                         f"can't update itself. Update it with this command: {command}")
        return out
    python = _base_python(prefix)
    if python is None:
        out["reason"] = ("The Python this install was made with isn't there anymore, so it can't update itself. "
                         "Run the installer again.")
        return out
    out.update(updatable=True, root=str(prefix.parent), program=str(prefix), python=str(python), command=None)
    return out


# ------------------------------------------------------------------ never during work
def busy_reasons(program: Path, *, now: datetime, processes: Callable[[Path], list] = swaphelper.processes_in,
                 allow: Iterable[int] = ()) -> list[str]:
    """Why an update can't start now, or an empty list."""
    reasons = []
    allowed = set(allow)
    try:
        others = [pid for pid, _path in processes(program) if pid not in allowed]
    except swaphelper.ProcessListError:
        others = None
        reasons.append("Couldn't list the programs running on this computer, so it can't tell whether FOLIO-Exchange "
                       "is running. Try again later.")
    if others:
        reasons.append(f"FOLIO-Exchange is running from {program}: a run, the page or another window. Try again "
                       "when it's finished.")
    if swaphelper.in_quiet_minutes(now):
        reasons.append("Scheduled runs start between minutes 5 and 15 of each hour. Try the update again after "
                       "that.")
    if marker_active(Path(program).parent, now):
        reasons.append("Another update started less than an hour ago and hasn't finished.")
    return reasons


# ------------------------------------------------------------------ downloading
def _no_account(r: Any) -> Any:
    """An ``auth`` that adds nothing, so requests never sends an account from the person's ``.netrc``."""
    return r


def download_session() -> requests.Session:
    """A session that sends GitHub only what ``_download`` sets: no default headers and no ``.netrc`` account.
    The system's proxy settings still apply."""
    s = requests.Session()
    s.headers.clear()
    s.auth = _no_account
    s.max_redirects = 0
    return s


def _size(limit: int) -> str:
    return f"{limit // (1024 * 1024)} MB" if limit >= 1024 * 1024 else f"{limit // 1024} KB"


def _download(http: Any, url: str, dest: Path, limit: int, what: str) -> None:
    """One file from GitHub to ``dest``, a new file. Redirects are followed by hand, to ``HOSTS`` only."""
    for _hop in range(MAX_REDIRECTS + 1):
        parts = urlparse(url)
        if parts.scheme != "https" or parts.hostname not in HOSTS:
            raise Stopped("download", f"GitHub sent the download of {what} to {parts.hostname or 'another site'}, "
                                      f"which isn't one of GitHub's download sites. {DELETED}")
        try:
            r = http.get(url, stream=True, allow_redirects=False, timeout=(CONNECT_SECONDS, STALL_SECONDS),
                         headers={"User-Agent": f"FOLIO-Exchange/{__version__}",
                                  "Accept": "application/octet-stream"})
        except requests.RequestException as exc:
            raise Stopped("download", f"Couldn't reach GitHub to download {what} ({type(exc).__name__}). Check "
                                      "this computer's internet connection or proxy settings.") from None
        if r.status_code not in (301, 302, 303, 307, 308):
            break
        url = urljoin(url, (getattr(r, "headers", None) or {}).get("Location") or "")
        _close(r)
    else:
        raise Stopped("download", f"GitHub redirected the download of {what} too many times. {DELETED}")
    try:
        if r.status_code == 404:
            raise Stopped("download", f"The release doesn't have {what}, so it can't be checked. {DELETED}")
        if r.status_code != 200:
            raise Stopped("download", f"GitHub answered {r.status_code} for {what}. Try again later.")
        try:
            length = int((getattr(r, "headers", None) or {}).get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > limit:
            raise Stopped("download", f"{what} is larger than {_size(limit)}, which is more than an update should "
                                      f"be. {DELETED}")
        got = 0
        with open(dest, "xb") as fh:
            try:
                for chunk in r.iter_content(64 * 1024):
                    got += len(chunk)
                    if got > limit:
                        raise Stopped("download", f"{what} is larger than {_size(limit)}, which is more "
                                                  f"than an update should be. {DELETED}")
                    fh.write(chunk)
            except requests.RequestException:
                raise Stopped("download", f"The download of {what} stopped, or got nothing for "
                                          f"{STALL_SECONDS} seconds. {DELETED}") from None
    finally:
        _close(r)


def _close(r: Any) -> None:
    close = getattr(r, "close", None)
    if close:
        close()


# ------------------------------------------------------------------ the project's signature
def _public_key(key: Any):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if isinstance(key, Ed25519PublicKey):
        return key
    raw = key
    if isinstance(key, str):
        try:
            raw = base64.b64decode(key.strip(), validate=True)
        except (binascii.Error, ValueError):
            raw = b""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) != 32:
        raise Stopped("key", "The release key in this version isn't a valid key, so it can't check an update. "
                             f"Nothing was downloaded. Get the new version from {RELEASES}.")
    return Ed25519PublicKey.from_public_bytes(bytes(raw))


def _signature(raw: bytes) -> bytes | None:
    if len(raw) == 64:
        return raw
    try:
        sig = base64.b64decode(raw.strip(), validate=True)
    except (binascii.Error, ValueError):
        return None
    return sig if len(sig) == 64 else None


def signed_sums(sums: bytes, sig: bytes, key: Any) -> dict[str, str]:
    """The checksums in ``SHA256SUMS`` once its signature checks out against ``key``, by file name."""
    from cryptography.exceptions import InvalidSignature

    public = _public_key(key)
    signature = _signature(sig)
    try:
        if signature is None:
            raise InvalidSignature()
        public.verify(signature, sums)
    except InvalidSignature:
        raise Stopped("verify", "The download isn't signed with FOLIO-Exchange's release key. " + DELETED) from None
    out = {}
    for line in sums.decode("utf-8", "replace").splitlines():
        m = _SUM_LINE.match(line.strip())
        if m:
            out[m[2].strip()] = m[1].lower()
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def fetch_release(tag: str, folder: Path, *, key: Any, http: Any) -> Path:
    """Download and check a release into ``folder``: the signed checksums first, then the zip. Returns the
    checked zip. Everything is under a temporary name until every check passes, and ``folder`` is emptied on
    any failure, so nothing unchecked is left to run."""
    version = tag[1:]
    base = f"{RELEASES}/download/{tag}/"
    name = zip_name(version)
    parts = {n: folder / (n + PART) for n in (SUMS, SIG, name)}
    try:
        _download(http, base + SUMS, parts[SUMS], MAX_SMALL_BYTES, "the checksums (SHA256SUMS)")
        _download(http, base + SIG, parts[SIG], MAX_SMALL_BYTES, "the signature (SHA256SUMS.sig)")
        sums = signed_sums(parts[SUMS].read_bytes(), parts[SIG].read_bytes(), key)
        want = sums.get(name)
        if not want:
            raise Stopped("verify", f"The signed checksums don't list {name}. {DELETED}")
        _download(http, base + name, parts[name], MAX_ZIP_BYTES, name)
        if not hmac.compare_digest(_sha256(parts[name]), want):
            raise Stopped("verify", f"{name} doesn't match its signed checksum. {DELETED}")
        for n, part in parts.items():
            os.replace(part, folder / n)
        return folder / name
    except BaseException:
        for n, part in parts.items():
            part.unlink(missing_ok=True)
            (folder / n).unlink(missing_ok=True)
        raise


# ------------------------------------------------------------------ a copy of each site
def known_sites() -> list[Path]:
    """The sites the tray watches and the recent ones, without repeats."""
    from .desktop import places  # no Qt

    out: list[Path] = []
    for p in [*places.watched_sites(), *places.recent_sites()]:
        if not any(os.path.normcase(os.path.abspath(p)) == os.path.normcase(os.path.abspath(q)) for q in out):
            out.append(p)
    return out


def _site_parts(root: Path) -> tuple[Path, Path | None, Path]:
    """The site's feeds folder, its own recipes folder (None when it has none) and its history."""
    try:
        site = load_site(root)
        own = site.recipe_dirs[0] if site.recipe_dirs else None
        if own is not None and os.path.normcase(os.path.abspath(own)) in (
                os.path.normcase(os.path.abspath(BUNDLED_RECIPES)), os.path.normcase(os.path.abspath(root))):
            own = None
        return site.feeds_dir, own, site.history_path
    except Exception:  # noqa: BLE001 - a site whose settings don't load is still copied, from the usual places
        return root / "feeds", root / "recipes", root / "data" / "history.sqlite"


def _backup(src: Path, dest: Path) -> None:
    """A consistent copy of a database another program may be writing, through SQLite's backup API."""
    source = sqlite3.connect(f"{Path(src).resolve().as_uri()}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(str(dest))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


_NEVER = shutil.ignore_patterns("keys", "*.key", "*.sqlite", "*.sqlite-*", "*.tmp")


def site_copy_folder(dest: Path, root: Path) -> Path:
    from .ui import site_id

    return Path(dest) / site_id(root)[:16]


def copy_sites(sites: Iterable[Path], dest: Path, *, version: str = __version__) -> dict:
    """Copy each site's settings, feeds, own recipes and history into ``dest``. Not the cache, run folders,
    logs or ``keys``. A folder with no site.yaml is skipped. Raises ``Stopped`` for a site that can't be
    copied."""
    copied, skipped = [], []
    for root in sites:
        root = Path(root)
        if not (root / SITE_FILE).is_file():
            skipped.append(str(root))
            continue
        target = site_copy_folder(dest, root)
        try:
            if target.exists():
                shutil.rmtree(target)
            target.mkdir(parents=True)
            shutil.copy2(root / SITE_FILE, target / SITE_FILE)
            feeds, recipes, history = _site_parts(root)
            if feeds.is_dir():
                shutil.copytree(feeds, target / "feeds", ignore=_NEVER)
            if recipes is not None and recipes.is_dir():
                shutil.copytree(recipes, target / "recipes", ignore=_NEVER)
            if history.is_file():
                _backup(history, target / "history.sqlite")
            (target / "site.json").write_text(json.dumps({
                "folder": str(root), "version": version,
                "copied": datetime.now(timezone.utc).isoformat(timespec="seconds")}, indent=1), encoding="utf-8")
        except (OSError, sqlite3.Error) as exc:
            shutil.rmtree(target, ignore_errors=True)
            raise Stopped("copy", f"The site {root} couldn't be copied ({exc}). Nothing was updated. Fix the site "
                                  "or close what's using it, then try again.") from None
        copied.append(str(root))
    return {"copied": copied, "skipped": skipped}


# ------------------------------------------------------------------ building the new environment
def _has_qt(program: Path) -> bool:
    return any(program.glob("Lib/site-packages/PySide6*")) or any(program.glob("lib/python*/site-packages/PySide6*"))


def _constraints(archive: Path, work: Path) -> Path | None:
    """The zip's own constraints.txt (the tested versions), as install.ps1 uses it."""
    with zipfile.ZipFile(archive) as z:
        names = [n for n in z.namelist() if n.count("/") == 1 and n.endswith("/constraints.txt")]
        if not names:
            return None
        out = work / "constraints.txt"
        out.write_bytes(z.read(names[0]))
        return out


def build_environment(base_python: Path, archive: Path, target: Path, work: Path, *, desktop: bool,
                      run: Callable = subprocess.run) -> Path:
    """Make a Python environment at ``target`` with FOLIO-Exchange from the checked ``archive``, as
    install.ps1 does: the same base Python, the zip's own constraints.txt, and Qt when the current program has
    it. Returns FOLIO-Exchange's wheel. Dependencies come from PyPI."""
    def step(argv: list, what: str) -> None:
        r = run([str(a) for a in argv], capture_output=True, text=True, encoding="utf-8", errors="replace",
                stdin=subprocess.DEVNULL, timeout=1800, cwd=str(work),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            last = [ln for ln in f"{r.stdout or ''}\n{r.stderr or ''}".splitlines() if ln.strip()]
            raise Stopped("build", f"{what} failed ({last[-1].strip() if last else r.returncode}).")

    step([base_python, "-I", "-X", "utf8", "-m", "venv", target], "Making the new program folder")
    python = swaphelper.env_python(target)
    pip = [python, "-I", "-X", "utf8", "-m", "pip"]
    quiet = ["--no-input", "--disable-pip-version-check"]
    wheels = work / "wheel"
    wheels.mkdir(exist_ok=True)
    step([*pip, "wheel", *quiet, "--no-deps", "--no-cache-dir", "-w", wheels, archive], "Building FOLIO-Exchange")
    found = sorted(wheels.glob("folio_exchange-*.whl"))
    if len(found) != 1:
        raise Stopped("build", "Building FOLIO-Exchange didn't give the one wheel expected.")
    constraints = _constraints(archive, work)
    pinned = ["-c", constraints] if constraints else []
    spec = f"folio-exchange{'[desktop]' if desktop else ''} @ {found[0].resolve().as_uri()}"
    step([*pip, "install", *quiet, *pinned, spec], "Installing FOLIO-Exchange and the parts it needs")
    return found[0]


# ------------------------------------------------------------------ the commands' program path
_ZIP_START = b"PK\x03\x04"


def _retarget(data: bytes, old: bytes, new: bytes, *, exe: bool) -> bytes | None:
    """``data`` with the old environment's path in its ``#!`` line changed to the new one, or None when it has
    no such line. A pip command on Windows is a small launcher, then that line, then a zip of the script; on
    other systems it's a text script whose first line it is."""
    flags = re.IGNORECASE if os.name == "nt" else 0
    pattern = re.compile(rb'#!"?(' + re.escape(old) + rb")[^\r\n]*\r?\n", flags)
    if not exe:
        m = pattern.match(data)
    else:
        m = next((m for m in reversed(list(pattern.finditer(data))) if data.startswith(_ZIP_START, m.end())), None)
    if m is None:
        return None
    return data[:m.start(1)] + new + data[m.end(1):]


def retarget_scripts(env: Path, final: Path) -> list[str]:
    """Point the commands in ``env`` (built at ``program.new``) at ``final`` (``program``) before the swap.
    pip writes the full path of the environment's Python into each command it makes, so without this,
    ``folio-exchange.exe`` would name a folder that's gone once the folders are swapped. Returns the commands
    changed. Raises ``Stopped`` when FOLIO-Exchange's own commands can't be changed."""
    scripts = swaphelper.scripts_dir(env)
    old = (str(Path(env)) + os.sep).encode("utf-8")
    new = (str(Path(final)) + os.sep).encode("utf-8")
    changed = []
    for f in sorted(scripts.iterdir()):
        if not f.is_file():
            continue
        data = f.read_bytes()
        out = _retarget(data, old, new, exe=data[:2] == b"MZ")
        if out is None:
            continue
        tmp = f.with_name(f.name + ".tmp")
        tmp.write_bytes(out)
        shutil.copymode(f, tmp)
        os.replace(tmp, f)
        changed.append(f.name)
    own = {swaphelper.program_exe(env).name}
    if (scripts / swaphelper.app_exe_name()).is_file():
        own.add(swaphelper.app_exe_name())
    if not own <= set(changed):
        raise Stopped("check", "FOLIO-Exchange was built, but its commands couldn't be pointed at the program "
                               "folder. Nothing was changed.")
    return changed


# ------------------------------------------------------------------ prepare, start the swap, results
def updates_folder() -> Path:
    from .desktop import places  # no Qt

    return places.local_dir() / UPDATES


def _prune(folder: Path, keep: set[str]) -> None:
    """Delete what an earlier update left: its download and its site copies. Called once a new download has
    passed its checks, so there's always one way back."""
    for p in folder.iterdir():
        if p.name in keep or not p.is_dir():
            continue
        if _VERSION_DIR.match(p.name) or p.name.startswith("from-"):
            shutil.rmtree(p, ignore_errors=True)


def prepare(tag: str, *, key: Any, install: dict | None = None, current: str = __version__,
            http: Any = None, builder: Callable | None = None, run: Callable = subprocess.run,
            sites: Iterable[Path] | None = None, now: datetime | None = None,
            processes: Callable[[Path], list] = swaphelper.processes_in, allow_pids: Iterable[int] | None = None,
            updates_dir: Path | None = None, protect: Callable | None = None) -> dict:
    """Get the release ``tag`` ready to swap in: every check, the download, the site copies and the new
    environment. Nothing is installed yet, and the old version stays the one that runs. Returns
    ``ok``, ``stage``, ``done``, ``left``, ``reason`` and, when ready, ``plan`` (the file ``start_swap`` takes)."""
    version = (tag or "")[1:]
    done: list[str] = []
    result: dict[str, Any] = {"ok": False, "version": version, "from": current, "stage": "install",
                              "program": "old", "done": done, "left": [], "reason": None, "plan": None,
                              "command": None}
    install = install if install is not None else find_install(version=version or None)
    program = Path(install["program"]) if install.get("program") else None
    new = program.parent / swaphelper.NEW if program else None
    folder: Path | None = None
    stage_dirs: list[Path] = []
    try:
        if not install.get("updatable"):
            result["command"] = install.get("command")
            raise Stopped("install", install["reason"])
        if key is None or key == "" or key == b"":
            raise Stopped("key", "This version has no release key, so it can't check an update. Nothing was "
                                 f"downloaded. Get the new version from {RELEASES}.")
        _public_key(key)
        wanted, have = tag_version(tag), parse_version(current)
        if wanted is None:
            raise Stopped("version", f"{tag} isn't a release tag such as v1.2.3. Nothing was downloaded.")
        if have is not None and wanted <= have:
            raise Stopped("version", f"{version} isn't newer than the version you have ({current}), so it wasn't "
                                     "installed.")
        now = now or datetime.now(timezone.utc).astimezone()
        allow = set(allow_pids) if allow_pids is not None else swaphelper.own_chain(program, processes=processes)
        reasons = busy_reasons(program, now=now, processes=processes, allow=allow)
        if reasons:
            raise Stopped("busy", " ".join(reasons) + " Nothing was changed.")
        outgoing = program.parent / swaphelper.OUTGOING
        if outgoing.exists():
            raise Stopped("busy", f"An earlier update didn't finish, and {outgoing} holds the version from before "
                                  "it. Run the installer again, then delete that folder.")

        updates = Path(updates_dir) if updates_dir is not None else updates_folder()
        updates.mkdir(parents=True, exist_ok=True)
        if protect is None:
            from .setupops import _owner_only as protect
        protect(updates, folder=True)
        with RunLock(updates / "update.lock"):
            folder = updates / version
            if folder.exists():
                shutil.rmtree(folder)
            folder.mkdir()
            stage_dirs.append(folder)
            result["stage"] = "download"
            archive = fetch_release(tag, folder, key=key, http=http if http is not None else download_session())
            done.append(f"Downloaded FOLIO-Exchange {version} and checked its signature.")
            stage_dirs.remove(folder)
            _prune(updates, {version, f"from-{current}", HELPER})

            result["stage"] = "copy"
            copies = updates / f"from-{current}"
            copies.mkdir(exist_ok=True)
            got = copy_sites(known_sites() if sites is None else sites, copies, version=current)
            if got["copied"]:
                done.append(f"Copied {len(got['copied'])} site(s) to {copies}.")

            result["stage"] = "build"
            if new.exists():
                shutil.rmtree(new)
                if new.exists():
                    raise Stopped("build", f"{new} is left from an earlier update and couldn't be removed. "
                                           "Delete it, then try again.")
            work = folder / "build"
            work.mkdir()
            stage_dirs.append(new)
            build = builder or build_environment
            build(Path(install["python"]), archive, new, work, desktop=_has_qt(program))
            result["stage"] = "check"
            problem = swaphelper.version_problem(run, new, version)
            if problem:
                raise Stopped("check", f"FOLIO-Exchange {version} was built, but {problem}. Nothing was changed.")
            retarget_scripts(new, program)
            done.append(f"Built FOLIO-Exchange {version} in {new}.")
            stage_dirs.remove(new)

            plan = {"format": swaphelper.PLAN_FORMAT, "root": str(program.parent), "new": str(new),
                    "version": version, "from": current,
                    "python": install["python"], "result": str(updates / RESULT), "restart": []}
            plan_path = folder / PLAN
            plan_path.write_text(json.dumps(plan, indent=1), encoding="utf-8")
            result.update(ok=True, stage="ready", plan=str(plan_path))
            result["left"].append(f"FOLIO-Exchange closes, switches to {version} and opens again.")
            return result
    except LockBusy:
        result.update(stage="busy", reason="Another update is being prepared. Wait for it to finish.")
    except Stopped as exc:
        result.update(stage=exc.stage, reason=exc.reason)
        result["left"] += exc.left
    except (OSError, sqlite3.Error) as exc:
        result["reason"] = f"The update stopped ({exc}). Nothing was changed."
    for p in stage_dirs:
        shutil.rmtree(p, ignore_errors=True)
    if result["stage"] in ("download", "verify") and folder is not None:
        shutil.rmtree(folder, ignore_errors=True)
    if result["stage"] not in ("install", "key", "version"):
        done.append(f"FOLIO-Exchange {current} is still installed.")
    log.info("Update to %s stopped at %s: %s", version, result["stage"], result["reason"])
    return result


def _read_plan(plan_path: str | os.PathLike) -> dict:
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    if plan.get("format") != swaphelper.PLAN_FORMAT:
        raise FxError("The update's plan is from another version of FOLIO-Exchange.")
    return plan


def swap(plan_path: str | os.PathLike, **kw: Any) -> dict:
    """Swap the folders in this process (tests and the command line; the app uses ``start_swap``). Writes the
    marker first, as ``start_swap`` does, and the result file after. ``kw`` goes to ``swaphelper.swap``."""
    plan = _read_plan(plan_path)
    write_marker(plan["root"], plan["version"], kw["now"]() if "now" in kw else None)
    result = swaphelper.swap(plan, **kw)
    _write_result(plan, result)
    return result


def _write_result(plan: dict, result: dict) -> None:
    out = Path(plan["result"])
    out.write_text(json.dumps({**result, "finished": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                              indent=1), encoding="utf-8")


def start_swap(plan_path: str | os.PathLike, *, restart: Iterable[list[str]] = (), now: datetime | None = None,
               launch: Callable = subprocess.Popen) -> dict:
    """Start the swap from outside the program folder: copy ``swaphelper.py`` into the updates folder and run
    it with the install's base Python. The caller quits right after; the helper waits for it. ``restart`` lists
    the programs (from the program folder) to start again afterwards."""
    plan = _read_plan(plan_path)
    now = now or datetime.now(timezone.utc).astimezone()
    if swaphelper.in_quiet_minutes(now):
        return {"ok": False, "stage": "busy", "program": "old", "done": [], "left": [],
                "reason": "Scheduled runs start between minutes 5 and 15 of each hour. Try the update again after "
                          "that."}
    plan["restart"] = [list(map(str, argv)) for argv in restart]
    Path(plan_path).write_text(json.dumps(plan, indent=1), encoding="utf-8")
    helper_dir = Path(plan["result"]).parent / HELPER
    helper_dir.mkdir(parents=True, exist_ok=True)
    helper = helper_dir / "swaphelper.py"
    shutil.copyfile(Path(swaphelper.__file__), helper)
    Path(plan["result"]).unlink(missing_ok=True)
    write_marker(plan["root"], plan["version"], now)
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | \
        getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        launch([plan["python"], "-I", "-X", "utf8", str(helper), str(plan_path)], cwd=str(helper_dir),
               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
               creationflags=flags)
    except OSError as exc:
        marker_path(plan["root"]).unlink(missing_ok=True)
        return {"ok": False, "stage": "swap", "program": "old", "done": [], "left": [],
                "reason": f"The update couldn't start ({exc.strerror or exc}). Nothing was changed."}
    return {"ok": True, "stage": "swap", "program": "old", "done": [],
            "left": [f"FOLIO-Exchange closes, switches to {plan['version']} and opens again."], "reason": None}


def last_result(updates_dir: Path | None = None) -> dict | None:
    """What the last swap did (``swaphelper``'s result), or None when no swap has finished."""
    path = (Path(updates_dir) if updates_dir is not None else updates_folder()) / RESULT
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
