"""The in-place updater (selfupdate.py, swaphelper.py): a faked GitHub, a throwaway key pair made in memory, a
temporary install folder and a fake environment builder. Nothing goes online, nothing real is installed, and no
real key is used."""
import ast
import base64
import hashlib
import io
import json
import os
import sqlite3
import sys
import types
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sitekit import make_site

from folio_exchange import selfupdate, swaphelper
from folio_exchange.cli import main

OLD, NEW = "0.2.0", "0.3.0"
TAG = f"v{NEW}"
CALM = datetime(2026, 11, 16, 10, 30, tzinfo=timezone.utc)      # minute 30: not when the scheduled task starts
BASE = f"{selfupdate.RELEASES}/download/{TAG}/"


# ------------------------------------------------------------------ fakes
def make_key():
    private = Ed25519PrivateKey.generate()
    return private, private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def source_zip(version=NEW) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"FOLIO-Exchange-{version}/pyproject.toml", f'[project]\nversion = "{version}"\n')
        z.writestr(f"FOLIO-Exchange-{version}/constraints.txt", "requests==2.32.3\n")
    return buf.getvalue()


def release(private, version=NEW, *, zip_bytes=None, sums_for=None):
    """A release's three assets, signed with ``private``."""
    zip_bytes = zip_bytes if zip_bytes is not None else source_zip(version)
    digest = hashlib.sha256(sums_for if sums_for is not None else zip_bytes).hexdigest()
    sums = f"{digest}  {selfupdate.zip_name(version)}\n{'0' * 64}  other.txt\n".encode()
    return {selfupdate.SUMS: sums, selfupdate.SIG: private.sign(sums), selfupdate.zip_name(version): zip_bytes}


class Response:
    def __init__(self, status, body=b"", headers=None, chunks=None, fail=None):
        self.status_code, self.headers, self._body, self._chunks, self._fail = status, headers or {}, body, chunks, fail
        self.closed = False

    def iter_content(self, size):
        if self._chunks is not None:
            yield from self._chunks
        else:
            for i in range(0, len(self._body), size):
                yield self._body[i:i + size]
        if self._fail:
            raise self._fail

    def close(self):
        self.closed = True


class FakeGitHub:
    """github.com redirects each asset to GitHub's download host, as it does for real release assets."""

    def __init__(self, files, *, host="release-assets.githubusercontent.com", special=None):
        self.files, self.host, self.special, self.urls, self.calls = files, host, special or {}, [], []

    def get(self, url, stream=False, allow_redirects=True, timeout=None, headers=None):
        self.urls.append(url)
        self.calls.append({"stream": stream, "allow_redirects": allow_redirects, "timeout": timeout,
                           "headers": headers})
        if url.startswith(BASE):
            name = url[len(BASE):]
            return Response(302, headers={"Location": f"https://{self.host}/asset/{name}"})
        name = url.rsplit("/", 1)[-1]
        if name in self.special:
            return self.special[name]
        if name in self.files:
            return Response(200, self.files[name], headers={"Content-Length": str(len(self.files[name]))})
        return Response(404, b"Not Found")


class Offline:
    def get(self, *a, **k):
        raise AssertionError("nothing should be downloaded")


def fake_env(folder: Path, version: str) -> None:
    scripts = swaphelper.scripts_dir(folder)
    scripts.mkdir(parents=True, exist_ok=True)
    (folder / "pyvenv.cfg").write_text(f"home = {folder.parent / 'base'}\n", encoding="utf-8")
    swaphelper.env_python(folder).write_text("fake", encoding="utf-8")
    for exe in (swaphelper.program_exe(folder), scripts / swaphelper.app_exe_name()):
        exe.write_bytes(launcher(folder))
    (folder / "VERSION").write_text(version, encoding="utf-8")


def launcher(env: Path, python: str | None = None) -> bytes:
    """A pip command as pip makes it: on Windows a launcher, a #! line naming the environment's Python and a
    zip of the script; elsewhere a text script with that #! line."""
    python = python or str(swaphelper.env_python(env))
    if os.name != "nt":
        return f"#!{python}\nimport sys\n".encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("__main__.py", "import sys\n")
    return b"MZ launcher stub #!not-this\n" + f'#!"{python}"\r\n'.encode() + buf.getvalue()


class Builder:
    """Stands in for build_environment: a fake environment reporting ``reports``, or a failure."""

    def __init__(self, reports=NEW, fail=False):
        self.reports, self.fail, self.calls = reports, fail, []

    def __call__(self, base_python, archive, target, work, *, desktop):
        self.calls.append({"python": base_python, "archive": archive, "target": target, "desktop": desktop})
        assert zipfile.is_zipfile(archive)
        if self.fail:
            fake_env(target, "0.0.0")
            raise selfupdate.Stopped("build", "Installing FOLIO-Exchange and the parts it needs failed (no network).")
        fake_env(target, self.reports)
        wheel = work / f"folio_exchange-{NEW}-py3-none-any.whl"
        wheel.write_bytes(b"wheel")
        return wheel


class Runner:
    """Stands in for subprocess.run: ``--version`` prints the VERSION file of the environment the program is in;
    nothing else is run."""

    def __init__(self, broken=()):
        self.broken, self.calls = set(broken), []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        env = Path(argv[0]).parents[1]
        assert Path(argv[0]).name == swaphelper.program_exe(env).name, argv
        if env.name in self.broken:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="ImportError: no module named yaml")
        version = (env / "VERSION").read_text(encoding="utf-8")
        return types.SimpleNamespace(returncode=0, stdout=f"folio-exchange {version}\n", stderr="")


@pytest.fixture
def key():
    return make_key()


@pytest.fixture
def install(tmp_path):
    root = tmp_path / "FOLIO-Exchange"
    program = root / "program"
    fake_env(program, OLD)
    (program / "Lib" / "site-packages" / "folio_exchange").mkdir(parents=True)
    base = root / "base"
    base.mkdir()
    python = base / ("python.exe" if os.name == "nt" else f"python{sys.version_info[0]}.{sys.version_info[1]}")
    python.write_text("fake", encoding="utf-8")
    return selfupdate.find_install(prefix=program, base_prefix=base,
                                   package_dir=program / "Lib" / "site-packages" / "folio_exchange")


@pytest.fixture
def a_site(tmp_path):
    root = make_site(tmp_path / "sites" / "lakeside", "https://folio.example.org")
    (root / "data").mkdir(exist_ok=True)
    with sqlite3.connect(root / "data" / "history.sqlite") as db:
        db.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, note TEXT)")
        db.execute("INSERT INTO runs (note) VALUES ('first delivery')")
    db.close()
    (root / "data" / "cache.sqlite").write_bytes(b"cache")
    (root / "keys").mkdir()
    (root / "keys" / "chq.key").write_text("PRIVATE", encoding="utf-8")
    (root / "out").mkdir(exist_ok=True)
    (root / "out" / "run-1.mrc").write_bytes(b"records")
    (root / "recipes" / "custom").mkdir(parents=True)
    (root / "recipes" / "custom" / "1.0.0.yaml").write_text("id: custom\n", encoding="utf-8")
    return root


def run_prepare(install, key, http, *, builder=None, run=None, sites=(), updates=None, **kw):
    kw.setdefault("now", CALM)
    kw.setdefault("processes", lambda folder: [])
    return selfupdate.prepare(TAG, key=key[1] if isinstance(key, tuple) else key, install=install, current=OLD,
                              http=http, builder=builder if builder is not None else Builder(),
                              run=run or Runner(), sites=sites,
                              updates_dir=updates or Path(install["root"]).parent / "app" / "updates",
                              protect=lambda *a, **k: None, **kw)


def old_program_untouched(install):
    program = Path(install["program"])
    assert (program / "VERSION").read_text(encoding="utf-8") == OLD
    assert not (program.parent / swaphelper.NEW).exists()
    assert not (program.parent / swaphelper.OUTGOING).exists()


def nothing_kept(result, install):
    updates = Path(install["root"]).parent / "app" / "updates"
    assert not (updates / NEW).exists(), "a download that failed a check is deleted"
    old_program_untouched(install)
    assert result["ok"] is False and result["program"] == "old"


# ------------------------------------------------------------------ the good path
def test_a_good_update_downloads_checks_copies_builds_and_swaps(install, key, a_site):
    http = FakeGitHub(release(key[0]))
    builder, run = Builder(), Runner()
    result = run_prepare(install, key, http, builder=builder, run=run, sites=[a_site])
    assert result["ok"], result["reason"]
    assert result["stage"] == "ready" and result["program"] == "old"
    assert result["done"][0] == f"Downloaded FOLIO-Exchange {NEW} and checked its signature."
    # downloads: the checksums and signature first, each redirected by hand to GitHub's download host
    assert [u.rsplit("/", 1)[-1] for u in http.urls[::2]] == ["SHA256SUMS", "SHA256SUMS.sig", f"folio-exchange-{NEW}.zip"]
    assert all(c["allow_redirects"] is False and c["stream"] for c in http.calls)
    assert all(c["timeout"] == (selfupdate.CONNECT_SECONDS, selfupdate.STALL_SECONDS) for c in http.calls)
    assert all(c["headers"]["User-Agent"].startswith("FOLIO-Exchange/") and set(c["headers"]) == {"User-Agent", "Accept"}
               for c in http.calls)
    updates = Path(install["root"]).parent / "app" / "updates"
    assert sorted(p.name for p in (updates / NEW).iterdir()) == sorted(
        ["SHA256SUMS", "SHA256SUMS.sig", f"folio-exchange-{NEW}.zip", "build", "plan.json"])
    # the site copy: settings, feeds, own recipes and the history; no cache, keys or run files
    copy = selfupdate.site_copy_folder(updates / f"from-{OLD}", a_site)
    assert (copy / "site.yaml").is_file() and (copy / "recipes" / "custom" / "1.0.0.yaml").is_file()
    assert json.loads((copy / "site.json").read_text(encoding="utf-8"))["folder"] == str(a_site)
    with sqlite3.connect(copy / "history.sqlite") as db:
        assert db.execute("SELECT note FROM runs").fetchall() == [("first delivery",)]
    db.close()
    names = {p.name for p in copy.rglob("*")}
    assert not names & {"keys", "chq.key", "cache.sqlite", "out", "run-1.mrc"}
    # the new environment, built beside the old one from the checked zip with the same base Python
    root = Path(install["root"])
    assert builder.calls[0]["target"] == root / "program.new"
    assert builder.calls[0]["python"] == Path(install["python"])
    assert (root / "program" / "VERSION").read_text(encoding="utf-8") == OLD

    swapped = selfupdate.swap(result["plan"], run=run, processes=lambda f: [], now=lambda: CALM)
    assert swapped["ok"] and swapped["program"] == "new", swapped["reason"]
    assert swapped["done"][0] == f"Updated to {NEW}."
    assert (root / "program" / "VERSION").read_text(encoding="utf-8") == NEW
    assert (root / "program.previous" / "VERSION").read_text(encoding="utf-8") == OLD     # a way back
    assert not (root / "program.new").exists() and not (root / "program.outgoing").exists()
    assert not (root / "updating.json").exists()
    # the commands were pointed at program before the swap, so they work the moment it has that name
    for name in (swaphelper.program_exe(root).name, swaphelper.app_exe_name()):
        data = (root / "program" / swaphelper.scripts_dir(root).name / name).read_bytes()
        assert str(swaphelper.env_python(root / "program")).encode() in data
        assert b"program.new" not in data
    assert selfupdate.last_result(updates)["ok"] is True


def test_the_next_update_replaces_the_previous_program_and_older_copies(install, key, a_site, tmp_path):
    root = Path(install["root"])
    updates = root.parent / "app" / "updates"
    (updates / "0.1.0").mkdir(parents=True)
    (updates / "from-0.1.0").mkdir()
    fake_env(root / "program.previous", "0.1.0")
    result = run_prepare(install, key, FakeGitHub(release(key[0])), sites=[a_site])
    assert not (updates / "0.1.0").exists() and not (updates / "from-0.1.0").exists()
    assert (root / "program.previous" / "VERSION").read_text(encoding="utf-8") == "0.1.0"    # until it succeeds
    selfupdate.swap(result["plan"], run=Runner(), processes=lambda f: [], now=lambda: CALM)
    assert (root / "program.previous" / "VERSION").read_text(encoding="utf-8") == OLD


def test_qt_is_installed_in_the_new_program_when_the_old_one_has_it(install, key):
    (Path(install["program"]) / "Lib" / "site-packages" / "PySide6").mkdir()
    builder = Builder()
    assert run_prepare(install, key, FakeGitHub(release(key[0])), builder=builder)["ok"]
    assert builder.calls[0]["desktop"] is True


# ------------------------------------------------------------------ what it refuses before downloading
def test_no_key_refuses_and_downloads_nothing(install):
    for no_key in (None, "", b""):
        result = run_prepare(install, no_key, Offline())
        assert result["stage"] == "key" and not result["ok"]
        assert result["reason"].startswith("This version has no release key, so it can't check an update.")
    old_program_untouched(install)


def test_a_key_that_is_not_an_ed25519_key_refuses(install):
    result = run_prepare(install, b"short", Offline())
    assert result["stage"] == "key" and "isn't a valid key" in result["reason"]


@pytest.mark.parametrize("version", [OLD, "0.1.9"])
def test_an_equal_or_lower_version_is_refused(install, key, version):
    result = selfupdate.prepare(f"v{version}", key=key[1], install=install, current=OLD, http=Offline(),
                                builder=Builder(), run=Runner(), sites=(), now=CALM, processes=lambda f: [],
                                updates_dir=Path(install["root"]) / "u", protect=lambda *a, **k: None)
    assert result["stage"] == "version" and result["reason"] == (
        f"{version} isn't newer than the version you have ({OLD}), so it wasn't installed.")
    old_program_untouched(install)


def test_a_tag_that_is_not_vxyz_is_refused(install, key):
    result = selfupdate.prepare("main", key=key[1], install=install, current=OLD, http=Offline(), sites=(),
                                now=CALM, updates_dir=Path(install["root"]) / "u", protect=lambda *a, **k: None)
    assert result["stage"] == "version"


def test_another_program_running_from_the_program_folder_holds_the_update(install, key):
    running = lambda folder: [(4242, str(Path(folder) / "Scripts" / "folio-exchange.exe"))]
    result = run_prepare(install, key, Offline(), processes=running, allow_pids={1})
    assert result["stage"] == "busy" and "FOLIO-Exchange is running from" in result["reason"]
    old_program_untouched(install)
    # the asking window and the tray are allowed
    assert run_prepare(install, key, FakeGitHub(release(key[0])), processes=running, allow_pids={4242})["ok"]


@pytest.mark.parametrize("minute", [5, 10, 15])
def test_the_minutes_when_the_scheduled_task_starts_hold_the_update(install, key, minute):
    result = run_prepare(install, key, Offline(), now=CALM.replace(minute=minute))
    assert result["stage"] == "busy" and "minutes 5 and 15" in result["reason"]
    assert run_prepare(install, key, FakeGitHub(release(key[0])), now=CALM.replace(minute=16))["ok"]


def test_an_unfinished_update_holds_the_next_one(install, key):
    selfupdate.write_marker(install["root"], NEW, CALM - timedelta(minutes=10))
    assert run_prepare(install, key, Offline())["stage"] == "busy"
    (Path(install["root"]) / "updating.json").unlink()
    (Path(install["root"]) / "program.outgoing").mkdir()
    result = run_prepare(install, key, Offline())
    assert result["stage"] == "busy" and "didn't finish" in result["reason"]


# ------------------------------------------------------------------ what it refuses while downloading
def test_a_signature_from_another_key_is_refused_and_nothing_runs(install, key):
    other, _ = make_key()
    builder = Builder()
    result = run_prepare(install, key, FakeGitHub(release(other)), builder=builder)
    assert result["stage"] == "verify"
    assert result["reason"] == ("The download isn't signed with FOLIO-Exchange's release key. The download didn't "
                                "pass its checks, so it was deleted. Nothing was installed.")
    assert builder.calls == []
    nothing_kept(result, install)


def test_a_missing_signature_is_refused(install, key):
    files = release(key[0])
    del files[selfupdate.SIG]
    builder = Builder()
    result = run_prepare(install, key, FakeGitHub(files), builder=builder)
    assert result["stage"] == "download" and result["reason"].startswith(
        "The release doesn't have the signature (SHA256SUMS.sig), so it can't be checked.")
    assert builder.calls == []
    nothing_kept(result, install)


def test_a_tampered_checksum_file_or_a_garbled_signature_is_refused(install, key):
    files = release(key[0])
    files[selfupdate.SUMS] = files[selfupdate.SUMS].replace(b"other.txt", b"other.exe")
    assert run_prepare(install, key, FakeGitHub(files))["stage"] == "verify"
    files = release(key[0])
    files[selfupdate.SIG] = b"not a signature"
    result = run_prepare(install, key, FakeGitHub(files))
    assert result["stage"] == "verify"
    nothing_kept(result, install)


def test_a_zip_that_differs_from_its_signed_checksum_is_refused(install, key):
    files = release(key[0], sums_for=b"another zip")
    builder = Builder()
    result = run_prepare(install, key, FakeGitHub(files), builder=builder)
    assert result["stage"] == "verify" and "doesn't match its signed checksum" in result["reason"]
    assert builder.calls == []
    nothing_kept(result, install)


def test_signed_checksums_that_do_not_list_this_version_are_refused(install, key):
    files = release(key[0], version="0.2.5")                 # an older release's signed files, served as 0.3.0
    files[selfupdate.zip_name(NEW)] = files.pop(selfupdate.zip_name("0.2.5"))
    result = run_prepare(install, key, FakeGitHub(files))
    assert result["stage"] == "verify" and "don't list folio-exchange-0.3.0.zip" in result["reason"]
    nothing_kept(result, install)


def test_a_redirect_to_a_host_off_the_list_stops_the_download(install, key):
    http = FakeGitHub(release(key[0]), host="downloads.example.com")
    builder = Builder()
    result = run_prepare(install, key, http, builder=builder)
    assert result["stage"] == "download" and "downloads.example.com" in result["reason"]
    assert not any("example.com" in u for u in http.urls), "the other host is never contacted"
    assert builder.calls == []
    nothing_kept(result, install)


def test_plain_http_is_refused(install, key, monkeypatch):
    monkeypatch.setattr(selfupdate, "RELEASES", selfupdate.RELEASES.replace("https:", "http:"))
    assert run_prepare(install, key, Offline())["stage"] == "download"


def test_an_oversized_download_is_refused(install, key):
    files = release(key[0])
    name = selfupdate.zip_name(NEW)
    big = Response(200, headers={"Content-Length": str(selfupdate.MAX_ZIP_BYTES + 1)})
    result = run_prepare(install, key, FakeGitHub(files, special={name: big}))
    assert result["stage"] == "download" and "larger than" in result["reason"]
    nothing_kept(result, install)
    # a server that doesn't say the size is stopped once it sends too much
    chunk = b"x" * (1024 * 1024)
    endless = Response(200, chunks=[chunk] * (selfupdate.MAX_ZIP_BYTES // len(chunk) + 2))
    result = run_prepare(install, key, FakeGitHub(files, special={name: endless}))
    assert result["stage"] == "download" and "larger than" in result["reason"]
    nothing_kept(result, install)


def test_a_stalled_download_is_refused(install, key):
    files = release(key[0])
    name = selfupdate.zip_name(NEW)
    stalled = Response(200, chunks=[b"PK\x03\x04"], fail=requests.ConnectionError("Read timed out."))
    builder = Builder()
    result = run_prepare(install, key, FakeGitHub(files, special={name: stalled}), builder=builder)
    assert result["stage"] == "download" and f"got nothing for {selfupdate.STALL_SECONDS} seconds" in result["reason"]
    assert builder.calls == []
    nothing_kept(result, install)


def test_github_unreachable_is_said_plainly(install, key):
    class Down:
        def get(self, *a, **k):
            raise requests.ConnectionError("no route")
    result = run_prepare(install, key, Down())
    assert result["stage"] == "download" and "Couldn't reach GitHub" in result["reason"]
    nothing_kept(result, install)


# ------------------------------------------------------------------ site copies, the build and the check
def test_a_site_that_cannot_be_copied_stops_the_update(install, key, a_site):
    (a_site / "data" / "history.sqlite").write_bytes(b"this is not a database at all" * 10)
    builder = Builder()
    result = run_prepare(install, key, FakeGitHub(release(key[0])), builder=builder, sites=[a_site])
    assert result["stage"] == "copy" and result["reason"].startswith(f"The site {a_site} couldn't be copied")
    assert builder.calls == []
    old_program_untouched(install)


def test_a_folder_that_is_no_longer_a_site_is_skipped(install, key, tmp_path):
    gone = tmp_path / "gone"
    assert run_prepare(install, key, FakeGitHub(release(key[0])), sites=[gone])["ok"]


def test_a_history_being_written_is_copied_whole(tmp_path, a_site):
    writer = sqlite3.connect(a_site / "data" / "history.sqlite")
    writer.execute("BEGIN")
    writer.execute("INSERT INTO runs (note) VALUES ('not committed')")
    got = selfupdate.copy_sites([a_site], tmp_path / "copies")
    writer.rollback()
    writer.close()
    assert got["copied"] == [str(a_site)]
    with sqlite3.connect(selfupdate.site_copy_folder(tmp_path / "copies", a_site) / "history.sqlite") as db:
        assert db.execute("SELECT note FROM runs").fetchall() == [("first delivery",)]
    db.close()


def test_a_build_that_fails_leaves_the_old_program(install, key):
    result = run_prepare(install, key, FakeGitHub(release(key[0])), builder=Builder(fail=True))
    assert result["stage"] == "build" and "no network" in result["reason"]
    assert result["done"][-1] == f"FOLIO-Exchange {OLD} is still installed."
    old_program_untouched(install)


def test_a_new_version_that_does_not_start_is_never_swapped_in(install, key):
    result = run_prepare(install, key, FakeGitHub(release(key[0])), run=Runner(broken={"program.new"}))
    assert result["stage"] == "check" and "didn't start" in result["reason"]
    old_program_untouched(install)
    result = run_prepare(install, key, FakeGitHub(release(key[0])), builder=Builder(reports="0.2.9"))
    assert result["stage"] == "check" and "reported version 0.2.9 instead of 0.3.0" in result["reason"]
    old_program_untouched(install)


# ------------------------------------------------------------------ the swap
@pytest.fixture
def ready(install, key):
    result = run_prepare(install, key, FakeGitHub(release(key[0])))
    assert result["ok"], result["reason"]
    return result["plan"]


def test_a_swap_that_fails_halfway_puts_the_old_program_back(install, ready):
    calls = []

    def rename(src, dst):
        calls.append((Path(src).name, Path(dst).name))
        if Path(src).name == "program.new":
            raise PermissionError(5, "Access is denied")
        os.rename(src, dst)

    result = selfupdate.swap(ready, run=Runner(), rename=rename, processes=lambda f: [], now=lambda: CALM)
    assert result["ok"] is False and result["program"] == "old"
    assert result["reason"].startswith("The new program folder couldn't be moved into place")
    assert result["reason"].endswith("The old version was put back.")
    assert calls[-1] == ("program.outgoing", "program")
    root = Path(install["root"])
    assert (root / "program" / "VERSION").read_text(encoding="utf-8") == OLD
    assert not (root / "updating.json").exists()


def test_a_program_folder_still_in_use_is_not_renamed(install, ready):
    def rename(src, dst):
        raise PermissionError(32, "The process cannot access the file")

    result = selfupdate.swap(ready, run=Runner(), rename=rename, processes=lambda f: [], now=lambda: CALM)
    assert result["program"] == "old" and "Nothing was changed" in result["reason"]
    assert (Path(install["root"]) / "program" / "VERSION").read_text(encoding="utf-8") == OLD


def test_a_new_version_that_fails_after_the_swap_is_rolled_back(install, ready):
    for run in (Runner(broken={"program"}),):
        plan = json.loads(Path(ready).read_text(encoding="utf-8"))
        if not Path(plan["new"]).exists():
            fake_env(Path(plan["new"]), NEW)
        result = selfupdate.swap(ready, run=run, processes=lambda f: [], now=lambda: CALM)
        assert result["program"] == "old" and result["reason"].endswith("The old version was put back.")
        root = Path(install["root"])
        assert (root / "program" / "VERSION").read_text(encoding="utf-8") == OLD
        assert not (root / "program.failed").exists() and not (root / "program.outgoing").exists()
        assert not (root / "program.previous").exists()


def test_the_swap_waits_for_the_app_to_quit_then_gives_up(install, ready):
    slept = []
    still = lambda folder: [(99, str(Path(folder) / "Scripts" / "pythonw.exe"))]
    result = selfupdate.swap(ready, run=Runner(), processes=still, now=lambda: CALM, sleep=slept.append,
                             wait_seconds=6)
    assert sum(slept) == 6 and result["program"] == "old" and "still running" in result["reason"]
    # one that quits while the helper waits
    answers = [still(Path(install["program"])), []]
    result = selfupdate.swap(ready, run=Runner(), processes=lambda f: answers.pop(0) if len(answers) > 1 else [],
                             now=lambda: CALM, sleep=slept.append)
    assert result["ok"]


def test_the_swap_never_runs_in_the_scheduled_tasks_minutes(install, ready):
    result = selfupdate.swap(ready, run=Runner(), processes=lambda f: [], now=lambda: CALM.replace(minute=7),
                             sleep=lambda s: None, wait_seconds=0)
    assert result["program"] == "old" and "minutes 5 and 15" in result["reason"]


def test_start_swap_runs_a_copy_of_the_helper_from_outside_the_program_folder(install, ready):
    launched = []
    result = selfupdate.start_swap(ready, restart=[[str(Path(install["program"]) / "Scripts" / "pythonw.exe")]],
                                   now=CALM, launch=lambda argv, **kw: launched.append((argv, kw)))
    assert result["ok"]
    argv, kw = launched[0]
    assert argv[0] == install["python"] and argv[1] == "-I"
    helper = Path(argv[4])
    plan = json.loads(Path(ready).read_text(encoding="utf-8"))
    assert argv[5:] == [str(ready), plan["root"], plan["result"]]
    assert helper.read_bytes() == Path(swaphelper.__file__).read_bytes()
    assert not str(helper).startswith(install["program"])
    assert Path(kw["cwd"]) == helper.parent
    assert selfupdate.marker_active(install["root"], CALM)
    assert json.loads(Path(ready).read_text(encoding="utf-8"))["restart"][0][0].endswith("pythonw.exe")
    assert not selfupdate.start_swap(ready, now=CALM.replace(minute=9), launch=lambda *a, **k: None)["ok"]


def test_the_helper_rolls_back_a_program_that_cannot_start_at_all(install, ready, monkeypatch):
    """The helper's own entry point, with real subprocess calls: the fake program's files aren't programs, so
    starting them fails, and the old folder must come back."""
    monkeypatch.setattr(swaphelper, "in_quiet_minutes", lambda now: False)
    monkeypatch.setattr(swaphelper, "processes_in", lambda folder: [])
    monkeypatch.setattr(swaphelper.swap, "__kwdefaults__", {**swaphelper.swap.__kwdefaults__,
                                                             "processes": lambda folder: []})
    plan = json.loads(Path(ready).read_text(encoding="utf-8"))
    assert swaphelper.main([ready, plan["root"], plan["result"]]) == 1
    result = json.loads(Path(plan["result"]).read_text(encoding="utf-8"))
    assert result["program"] == "old" and result["reason"].endswith("The old version was put back.")
    assert (Path(install["root"]) / "program" / "VERSION").read_text(encoding="utf-8") == OLD


def test_a_helper_that_fails_early_removes_the_marker_and_says_so(install, ready, monkeypatch):
    """Every entry point refuses while the marker is new, so a helper that stops before the swap (an
    unreadable plan, or a plan for another folder) must still remove it and write a result."""
    plan = json.loads(Path(ready).read_text(encoding="utf-8"))
    root, out = Path(plan["root"]), Path(plan["result"])
    for bad in ("not json", json.dumps({**plan, "root": str(root.parent / "elsewhere")})):
        selfupdate.write_marker(root, NEW)
        broken = Path(ready).with_name("broken.json")
        broken.write_text(bad, encoding="utf-8")
        assert swaphelper.main([str(broken), str(root), str(out)]) == 1
        assert not (root / "updating.json").exists()
        result = json.loads(out.read_text(encoding="utf-8"))
        assert result["ok"] is False and result["program"] == "old"
        assert result["reason"].startswith("The update's plan couldn't be read")
        assert (root / "program" / "VERSION").read_text(encoding="utf-8") == OLD
    # and when the swap itself raises something unexpected
    selfupdate.write_marker(root, NEW)
    monkeypatch.setattr(swaphelper, "swap", lambda plan: 1 / 0)
    assert swaphelper.main([ready, str(root), str(out)]) == 1
    assert not (root / "updating.json").exists()
    assert json.loads(out.read_text(encoding="utf-8"))["reason"].startswith("The update stopped (ZeroDivisionError")


def test_the_helper_needs_only_the_standard_library():
    tree = ast.parse(Path(swaphelper.__file__).read_text(encoding="utf-8"))
    names = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not any(isinstance(n, ast.ImportFrom) and n.level for n in ast.walk(tree)), "no relative imports"
    assert names <= set(sys.stdlib_module_names) | {"__future__"}


def test_processes_in_finds_this_one():
    folders = {Path(os.path.realpath(p)).parent for p in (sys.executable, getattr(sys, "_base_executable", ""))
               if p}
    if sys.platform not in ("win32", "linux"):
        pytest.skip("processes are listed on Windows and Linux")
    assert any(os.getpid() in [pid for pid, _ in swaphelper.processes_in(f)] for f in folders)


# ------------------------------------------------------------------ which installs can update
def test_a_pip_install_gets_the_command(tmp_path):
    out = selfupdate.find_install(prefix=tmp_path / "py", base_prefix=tmp_path / "py", version=NEW)
    assert not out["updatable"]
    assert out["command"] == ('pip install "folio-exchange[desktop] @ '
                              f'git+https://github.com/ByYudkowskysTentacle/FOLIO-Exchange.git@v{NEW}"')
    assert out["reason"].endswith(out["command"])
    venv = tmp_path / ".venv"
    fake_env(venv, OLD)
    assert not selfupdate.find_install(prefix=venv, base_prefix=tmp_path / "base")["updatable"]


def test_an_editable_install_gets_the_command(tmp_path):
    program = tmp_path / "program"
    fake_env(program, OLD)
    out = selfupdate.find_install(prefix=program, base_prefix=tmp_path / "base", package_dir=tmp_path / "src")
    assert not out["updatable"] and "outside its program folder" in out["reason"]


def test_a_program_whose_python_has_gone_says_to_run_the_installer(tmp_path):
    program = tmp_path / "program"
    fake_env(program, OLD)
    (program / "Lib").mkdir()
    out = selfupdate.find_install(prefix=program, base_prefix=tmp_path / "base", package_dir=program / "Lib")
    assert not out["updatable"] and out["reason"].endswith("Run the installer again.")


def test_this_test_run_is_not_an_install_it_can_update():
    assert selfupdate.find_install()["updatable"] is False


def test_prepare_refuses_a_pip_install_with_the_command(tmp_path, key):
    install = selfupdate.find_install(prefix=tmp_path, base_prefix=tmp_path, version=NEW)
    result = selfupdate.prepare(TAG, key=key[1], install=install, current=OLD, http=Offline())
    assert result["stage"] == "install" and result["command"].startswith("pip install")


def test_a_base64_key_and_signature_are_accepted(key):
    files = release(key[0])
    got = selfupdate.signed_sums(files[selfupdate.SUMS], base64.b64encode(files[selfupdate.SIG]) + b"\n",
                                 base64.b64encode(key[1]).decode())
    assert selfupdate.zip_name(NEW) in got


# ------------------------------------------------------------------ the marker and run --due
def test_the_marker_lasts_an_hour(tmp_path):
    selfupdate.write_marker(tmp_path, NEW, CALM)
    assert selfupdate.marker_active(tmp_path, CALM + timedelta(minutes=59))
    assert not selfupdate.marker_active(tmp_path, CALM + timedelta(minutes=61))
    assert not selfupdate.marker_active(tmp_path, CALM - timedelta(hours=2))      # dated well ahead: ignored
    (tmp_path / "updating.json").write_text("garbled", encoding="utf-8")         # falls back to its file time
    assert selfupdate.marker_active(tmp_path)


def test_run_due_skips_while_the_program_is_being_updated(tmp_path, monkeypatch, capsys):
    program = tmp_path / "program"
    program.mkdir()
    monkeypatch.setattr(sys, "prefix", str(program))
    selfupdate.write_marker(tmp_path, NEW)
    site = make_site(tmp_path / "lakeside", "https://folio.example.org")
    assert main(["-s", str(site), "run", "--due"]) == 0
    assert "being updated, so this scheduled pass is skipped" in capsys.readouterr().out
    selfupdate.write_marker(tmp_path, NEW, datetime.now(timezone.utc) - timedelta(hours=2))
    assert not selfupdate.update_in_progress()


# ------------------------------------------------------------------ review of 16 Nov: regressions
def test_no_entry_point_starts_while_the_program_is_being_swapped(tmp_path, monkeypatch, capsys):
    """Windows lets the program folder be renamed under a running program, so nothing may start from it
    during the swap: the command line (every command), the desktop app and the tray."""
    from folio_exchange import desktop
    from folio_exchange.desktop import app as desktop_app

    def opened(*a, **k):
        raise AssertionError("the app or the tray started during an update")

    monkeypatch.setattr(desktop_app, "run", opened)
    tray = pytest.importorskip("folio_exchange.desktop.tray")
    monkeypatch.setattr(tray, "run_tray", opened)
    program = tmp_path / "program"
    program.mkdir()
    monkeypatch.setattr(sys, "prefix", str(program))
    site = make_site(tmp_path / "lakeside", "https://folio.example.org")
    selfupdate.write_marker(tmp_path, NEW)
    for argv in (["-s", str(site), "feeds"], ["-s", str(site), "run", "--all"], ["init", str(tmp_path / "x")]):
        assert main(argv) == 1
        assert capsys.readouterr().err.strip() == "FOLIO-Exchange is being updated. Try again in a few minutes."
    assert not (tmp_path / "x").exists()
    assert desktop.main(["--site", str(site)]) == 1
    assert desktop.main(["--tray"]) == 1
    assert capsys.readouterr().err.count("FOLIO-Exchange is being updated.") == 2
    with pytest.raises(SystemExit) as stopped:           # --version answers: the swap's own check uses it
        main(["--version"])
    assert stopped.value.code == 0
    selfupdate.write_marker(tmp_path, NEW, datetime.now(timezone.utc) - timedelta(hours=2))
    assert main(["-s", str(site), "feeds"]) == 0           # an old marker, from an update that died, is ignored


def test_a_process_list_that_cannot_be_read_never_means_nothing_runs(install, key, ready, monkeypatch):
    def unreadable(folder):
        raise swaphelper.ProcessListError("access denied")

    result = run_prepare(install, key, Offline(), processes=unreadable, allow_pids={1})
    assert result["stage"] == "busy" and result["reason"].startswith("Couldn't list the programs running")
    result = selfupdate.swap(ready, run=Runner(), processes=unreadable, now=lambda: CALM, sleep=lambda s: None)
    assert result["program"] == "old" and result["reason"].startswith("Couldn't list the programs running")
    assert (Path(install["root"]) / "program" / "VERSION").read_text(encoding="utf-8") == OLD
    # the real listing raises too, rather than returning nothing
    name = "_windows_processes" if os.name == "nt" else "_proc_processes"
    monkeypatch.setattr(swaphelper, name, lambda: (_ for _ in ()).throw(OSError("denied")))
    with pytest.raises(swaphelper.ProcessListError):
        swaphelper.processes_in(Path(install["program"]))


def test_a_program_started_during_the_swap_puts_the_old_folder_back(install, ready):
    renamed = []

    def rename(src, dst):
        renamed.append(Path(src).name)
        os.rename(src, dst)

    def windows_listing(folder):
        """As Windows lists it: once program has been renamed, a program that started from it is still listed
        under program, the path it started from, and never under program.outgoing."""
        if renamed and Path(folder).name == "program":
            return [(77, str(Path(folder) / "Scripts" / "folio-exchange.exe"))]
        return []

    result = selfupdate.swap(ready, run=Runner(), processes=windows_listing, now=lambda: CALM, sleep=lambda s: None,
                             rename=rename)
    assert result["program"] == "old" and result["reason"].startswith("A program started from the program folder")
    assert renamed == ["program", "program.outgoing"]
    assert (Path(install["root"]) / "program" / "VERSION").read_text(encoding="utf-8") == OLD

    def caught(folder):
        return [(77, str(Path(folder) / "Scripts" / "folio-exchange.exe"))] if Path(folder).name == "program.outgoing" \
            else []

    slept = []
    result = selfupdate.swap(ready, run=Runner(), processes=caught, now=lambda: CALM, sleep=slept.append)
    assert result["program"] == "old" and result["reason"].startswith("A program started from the program folder")
    assert sum(slept) >= swaphelper.SETTLE_SECONDS
    root = Path(install["root"])
    assert (root / "program" / "VERSION").read_text(encoding="utf-8") == OLD
    assert (root / "program.new").is_dir() and not (root / "program.outgoing").exists()
    # one that quits within a few seconds (it found the marker) doesn't stop the update
    answers = [[(77, "x")], []]
    result = selfupdate.swap(ready, run=Runner(), now=lambda: CALM, sleep=lambda s: None,
                             processes=lambda f: (answers.pop(0) if answers else [])
                             if Path(f).name == "program.outgoing" else [])
    assert result["ok"], result["reason"]


def test_the_app_started_from_its_launcher_is_not_another_program(install, key):
    """Started as folio-exchange-app.exe, the app is three processes: the launcher and the environment's
    pythonw.exe (both in the program folder) and the base Python. None is another program to wait for."""
    program = Path(install["program"])
    me = os.getpid()
    running = [(10, str(program / "Scripts" / "folio-exchange-app.exe")), (11, str(program / "Scripts" / "pythonw.exe"))]
    tree = {me: 11, 11: 10, 10: 4}
    assert swaphelper.own_chain(program, processes=lambda f: running, parents=lambda: tree) == {me, 10, 11}
    import folio_exchange.swaphelper as helper_module

    original = helper_module.parent_pids
    helper_module.parent_pids = lambda: tree
    try:
        assert run_prepare(install, key, FakeGitHub(release(key[0])), processes=lambda f: running)["ok"]
        # a scheduled run started by Task Scheduler is not in the chain
        others = [*running, (20, str(program / "Scripts" / "folio-exchange.exe"))]
        assert run_prepare(install, key, Offline(), processes=lambda f: others)["stage"] == "busy"
    finally:
        helper_module.parent_pids = original


def test_parent_pids_finds_this_process_parent():
    if sys.platform not in ("win32", "linux"):
        pytest.skip("parents are read on Windows and Linux")
    assert swaphelper.parent_pids().get(os.getpid()) == os.getppid()


def test_commands_are_pointed_at_the_program_folder_before_the_swap(tmp_path):
    new, final = tmp_path / "program.new", tmp_path / "program"
    fake_env(new, NEW)
    (swaphelper.scripts_dir(new) / ("pip.exe" if os.name == "nt" else "pip")).write_bytes(launcher(new))
    (swaphelper.scripts_dir(new) / "activate.bat").write_text("set X=1\n", encoding="utf-8")
    changed = selfupdate.retarget_scripts(new, final)
    assert set(changed) == {swaphelper.program_exe(new).name, swaphelper.app_exe_name(),
                            "pip.exe" if os.name == "nt" else "pip"}
    data = swaphelper.program_exe(new).read_bytes()
    assert str(swaphelper.env_python(final)).encode() in data and b"program.new" not in data
    if os.name == "nt":
        assert data.startswith(b"MZ launcher stub #!not-this\n")           # only the line before the zip
        assert zipfile.ZipFile(io.BytesIO(data)).namelist() == ["__main__.py"]


def test_a_build_whose_commands_cannot_be_retargeted_is_never_swapped_in(install, key):
    class Odd(Builder):
        def __call__(self, *a, **k):
            wheel = super().__call__(*a, **k)
            swaphelper.program_exe(a[2]).write_bytes(b"no #! line here")
            return wheel

    result = run_prepare(install, key, FakeGitHub(release(key[0])), builder=Odd())
    assert result["stage"] == "check" and "couldn't be pointed at the program folder" in result["reason"]
    old_program_untouched(install)


def test_a_real_pip_command_runs_after_its_folder_is_renamed(tmp_path):
    """A real environment and a command made by distlib (pip's own): after retargeting and renaming the
    folder, the command starts from its new place and runs."""
    import subprocess

    distlib = pytest.importorskip("pip._vendor.distlib.scripts")
    new, final = tmp_path / "program.new", tmp_path / "program"
    made = subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(new)], capture_output=True, text=True,
                          timeout=120)
    assert made.returncode == 0, made.stderr
    (tmp_path / "fxprobe.py").write_text("def main():\n    print('probe ran')\n", encoding="utf-8")
    maker = distlib.ScriptMaker(None, str(swaphelper.scripts_dir(new)))
    maker.executable = str(swaphelper.env_python(new))
    maker.variants = {""}
    maker.make("folio-exchange = fxprobe:main")
    assert "folio-exchange" in " ".join(selfupdate.retarget_scripts(new, final))
    os.rename(new, final)
    out = subprocess.run([str(swaphelper.program_exe(final))], capture_output=True, text=True, timeout=60,
                         env={**os.environ, "PYTHONPATH": str(tmp_path)})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "probe ran"


def test_downloads_send_no_account_from_netrc_and_no_extra_headers(tmp_path, monkeypatch):
    netrc = tmp_path / "netrc"
    netrc.write_text("machine github.com login someone password not-a-real-token\n", encoding="utf-8")
    monkeypatch.setenv("NETRC", str(netrc))
    url = f"{BASE}SHA256SUMS"
    headers = {"User-Agent": "FOLIO-Exchange/0.2.0", "Accept": "application/octet-stream"}
    plain = requests.Session()
    assert "Authorization" in plain.prepare_request(requests.Request("GET", url, headers=headers)).headers, \
        "the test's .netrc reaches a plain session"
    s = selfupdate.download_session()
    sent = s.prepare_request(requests.Request("GET", url, headers=headers))
    assert dict(sent.headers) == headers
    settings = s.merge_environment_settings(url, {}, True, None, None)
    assert "auth" not in settings or settings.get("auth") is None


def test_prepare_downloads_through_the_bare_session(install, key, monkeypatch):
    used = []

    def session():
        used.append(True)
        return FakeGitHub(release(key[0]))

    monkeypatch.setattr(selfupdate, "download_session", session)
    monkeypatch.setattr(requests, "Session", lambda: (_ for _ in ()).throw(AssertionError("a plain session")))
    result = selfupdate.prepare(TAG, key=key[1], install=install, current=OLD, builder=Builder(), run=Runner(),
                                sites=(), now=CALM, processes=lambda f: [], allow_pids={1},
                                updates_dir=Path(install["root"]).parent / "app" / "updates",
                                protect=lambda *a, **k: None)
    assert result["ok"], result["reason"]
    assert used == [True]


def test_a_tag_with_a_trailing_newline_is_not_a_release_tag():
    assert selfupdate.tag_version("v0.3.0\n") is None
    assert selfupdate.tag_version("v0.3.0") == (0, 3, 0)
