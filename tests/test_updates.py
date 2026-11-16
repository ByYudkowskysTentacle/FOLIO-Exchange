"""The check for a newer version (updates.py, phase U0 of docs/updates-plan.md): which release is offered, when
GitHub is asked, what the request carries, the IT switches. GitHub is faked; nothing goes online."""
from __future__ import annotations

import json
import subprocess
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import pytest
import requests
from requests.adapters import BaseAdapter
from sitekit import make_feed

from folio_exchange import updates
from folio_exchange.cli import main
from folio_exchange.desktop import places
from folio_exchange.history import History

T0 = datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)
REAL_SESSION = updates._session          # kept before conftest replaces it for every test
HOUR = timedelta(hours=1)


def release(tag, *, draft=False, prerelease=False, body=""):
    return {"tag_name": tag, "draft": draft, "prerelease": prerelease, "body": body,
            "published_at": "2026-11-15T10:00:00Z", "html_url": "https://evil.example/elsewhere"}


def answer(status=200, data=None, headers=None, url=updates.RELEASES_URL):
    content = json.dumps([] if data is None else data).encode()
    return types.SimpleNamespace(status_code=status, content=content, text=content.decode(), headers=headers or {},
                                 url=url, json=lambda: json.loads(content))


class FakeGitHub:
    """Answers the releases request: ``answers`` in turn, then the last one again. Keeps every request."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers) or [answer()], []

    def get(self, url, timeout=None, headers=None, allow_redirects=True):
        self.calls.append({"url": url, "headers": dict(headers or {}), "allow_redirects": allow_redirects})
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, Exception):
            raise a
        return a


@pytest.fixture(autouse=True)
def checking_on(monkeypatch):
    """conftest turns checking off for every test; these turn it on, as version 0.2.0."""
    monkeypatch.delenv(updates.ENV, raising=False)
    monkeypatch.setattr(updates, "__version__", "0.2.0")


def saved() -> dict:
    return json.loads((places.config_dir() / updates.FILE).read_text(encoding="utf-8"))


# ------------------------------------------------------------------ which release
def test_the_newest_higher_version_is_chosen():
    listed = [release("v0.2.1"), release("v0.10.0"), release("v0.9.9"), release("v0.2.0"), release("v0.1.0")]
    assert updates.newest(listed, (0, 2, 0), True)["version"] == "0.10.0"          # by number, not by text or order


def test_drafts_are_never_used_and_prereleases_only_when_chosen():
    listed = [release("v0.5.0", draft=True), release("v0.4.0", prerelease=True), release("v0.3.0"),
              {"tag_name": "v0.6.0"}]                                              # says neither: counted as both
    assert updates.newest(listed, (0, 2, 0), True)["version"] == "0.4.0"
    assert updates.newest(listed, (0, 2, 0), False)["version"] == "0.3.0"


def test_never_a_lower_or_equal_version():
    listed = [release("v0.2.0"), release("v0.1.9"), release("v0.0.1")]
    assert updates.newest(listed, (0, 2, 0), True) is None
    assert updates.newest([release("v9.0.0")], None, True) is None                # this version unknown: nothing


@pytest.mark.parametrize("tag", ["0.3.0", "v0.3", "v0.3.0-rc1", "v0.3.0.1", "V0.3.0", " v0.3.0", "v0.3.0\n", "latest",
                                 "v1.x.0", None, 3])
def test_tags_that_are_not_v_x_y_z_are_ignored(tag):
    assert updates.tag_version(tag) is None
    assert updates.newest([release(tag)], (0, 2, 0), True) is None


def test_an_answer_that_is_not_a_list_of_releases_offers_nothing():
    for listed in (None, {"message": "Not Found"}, ["v9.0.0", 7, None]):
        assert updates.newest(listed, (0, 2, 0), True) is None


def test_this_versions_number():
    assert updates.running_version("0.3.0") == (0, 3, 0) and updates.running_version("0.3.0.dev1") == (0, 3, 0)
    assert updates.running_version("unknown") is None


# ------------------------------------------------------------------ when
def test_at_most_one_request_a_day_after_a_check_that_worked():
    gh = FakeGitHub(answer(data=[release("v0.3.0", body="Fixes a delivery bug.")]))
    assert updates.check(http=gh, now=T0) == {"checked": True, "problem": None}
    for later in (timedelta(minutes=1), 5 * HOUR, 23 * HOUR + timedelta(minutes=59)):
        assert updates.check(http=gh, now=T0 + later) == {"checked": False, "problem": None}
    assert len(gh.calls) == 1
    assert updates.check(http=gh, now=T0 + 24 * HOUR)["checked"] and len(gh.calls) == 2
    assert updates.status()["available"] == {"version": "0.3.0", "prerelease": False, "notes": "Fixes a delivery bug.",
                                              "page": f"https://github.com/{updates.REPO}/releases/tag/v0.3.0"}


def test_a_failed_check_is_tried_two_hours_later_and_at_most_four_times_a_day(caplog):
    gh = FakeGitHub(requests.ConnectionError("no route"))
    tried = [h for h in range(0, 30) if updates.check(http=gh, now=T0 + h * HOUR)["checked"]]
    assert tried == [0, 2, 4, 6, 24, 26, 28]                       # four in the first day, then the next day's
    assert "The check for updates didn't work" in caplog.text
    status = updates.status()
    assert status["failed"] and status["checked"] is None and status["available"] is None


def test_a_failed_check_after_one_that_worked_is_tried_again_two_hours_later():
    gh = FakeGitHub(answer(), requests.Timeout("slow"), answer(data=[release("v0.3.0")]))
    assert updates.check(http=gh, now=T0)["checked"]
    failed = updates.check(http=gh, now=T0 + 24 * HOUR)
    assert failed["checked"] and failed["problem"].startswith("Couldn't reach GitHub (Timeout).")
    assert not updates.check(http=gh, now=T0 + 25 * HOUR)["checked"]
    assert updates.check(http=gh, now=T0 + 26 * HOUR) == {"checked": True, "problem": None}
    assert saved().get("failed") is None and updates.status()["available"]["version"] == "0.3.0"


def test_retry_after_is_honored():
    gh = FakeGitHub(answer(429, headers={"Retry-After": str(3 * 3600)}), answer())
    assert updates.check(http=gh, now=T0)["problem"] == "GitHub is limiting requests from this network."
    assert not updates.check(http=gh, now=T0 + 2 * HOUR)["checked"]            # two hours would be too soon
    asked = updates.check(asked=True, http=gh, now=T0 + 2 * HOUR)             # even when the person asks
    assert asked == {"checked": False, "problem": "GitHub is limiting requests from this network. Try again in "
                                                  "about 60 minutes."}
    assert len(gh.calls) == 1
    assert updates.check(http=gh, now=T0 + 3 * HOUR) == {"checked": True, "problem": None}


def test_the_rate_limits_reset_time_is_honored():
    reset = int((T0 + 5 * HOUR).timestamp())
    gh = FakeGitHub(answer(403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)}), answer())
    assert updates.check(http=gh, now=T0)["checked"]
    assert [h for h in range(1, 7) if updates.check(http=gh, now=T0 + h * HOUR)["checked"]] == [5]


def test_a_wait_of_more_than_a_day_is_not_believed():
    gh = FakeGitHub(answer(503, headers={"Retry-After": str(90 * 86400)}), answer())
    assert updates.check(http=gh, now=T0)["problem"] == "GitHub answered 503."
    assert updates.check(http=gh, now=T0 + 2 * HOUR) == {"checked": True, "problem": None}


def test_a_check_the_person_asks_for_does_not_wait_for_the_day():
    gh = FakeGitHub(answer())
    assert updates.check(http=gh, now=T0)["checked"]
    assert updates.check(asked=True, http=gh, now=T0 + HOUR) == {"checked": True, "problem": None}
    assert len(gh.calls) == 2


def test_a_check_under_way_elsewhere_is_not_doubled():
    """The tray and a window start together when someone signs in: one of them asks."""
    updates._change(checking=updates._stamp(T0))
    gh = FakeGitHub(answer())
    assert not updates.check(http=gh, now=T0 + timedelta(seconds=30))["checked"]
    assert updates.check(http=gh, now=T0 + timedelta(minutes=3))["checked"]    # one that never ended doesn't block


def test_a_clock_set_back_or_a_damaged_file_does_not_stop_checks():
    updates._change(last_ok=updates._stamp(T0 + 400 * 24 * HOUR), failed=["soon", 7], wait_until="never")
    assert updates.due(T0)
    (places.config_dir() / updates.FILE).write_text("{not json", encoding="utf-8")
    assert updates.load() == {} and updates.due(T0)
    (places.config_dir() / updates.FILE).write_text("[1, 2]", encoding="utf-8")
    assert updates.load() == {} and updates.status()["available"] is None


# ------------------------------------------------------------------ the request
def test_redirects_are_followed_by_hand_and_only_within_api_github_com():
    moved = answer(301, headers={"Location": "/repositories/123/releases"})
    gh = FakeGitHub(moved, answer(data=[release("v0.3.0")]))
    assert updates.check(http=gh, now=T0) == {"checked": True, "problem": None}
    assert [c["url"] for c in gh.calls] == [updates.RELEASES_URL, "https://api.github.com/repositories/123/releases"]
    assert not any(c["allow_redirects"] for c in gh.calls)


@pytest.mark.parametrize("where", ["https://example.org/releases", "http://api.github.com/x",
                                   "https://api.github.com.example.org/x", "https://github.com/x"])
def test_a_redirect_anywhere_else_stops_the_check(where):
    gh = FakeGitHub(answer(302, headers={"Location": where}), answer(data=[release("v9.0.0")]))
    result = updates.check(http=gh, now=T0)
    assert result["checked"] and "another site" in result["problem"]
    assert len(gh.calls) == 1 and updates.status()["available"] is None


def test_endless_redirects_and_odd_answers_fail_quietly():
    loop = FakeGitHub(answer(302, headers={"Location": updates.RELEASES_URL}))
    assert updates.check(http=loop, now=T0)["problem"] == "GitHub redirected the check too many times."
    assert len(loop.calls) == 4
    odd = FakeGitHub(answer(data={"message": "Not Found"}))
    assert updates.check(asked=True, http=odd, now=T0)["problem"] == "GitHub's answer wasn't a list of releases."
    big = answer()
    big.content = b" " * (updates.MAX_BYTES + 1)
    assert updates.check(asked=True, http=FakeGitHub(big), now=T0)["problem"] == "GitHub's answer was too large."


def test_the_repository_is_fixed_in_the_code():
    updates._change(repo="someone/else", url="https://example.org/releases", check=True)
    gh = FakeGitHub(answer())
    updates.check(http=gh, now=T0)
    assert gh.calls[0]["url"] == "https://api.github.com/repos/ByYudkowskysTentacle/FOLIO-Exchange/releases"


class Wire(BaseAdapter):
    """Stands where the network would be: keeps what requests was about to send, and answers no releases."""

    def __init__(self):
        super().__init__()
        self.sent: list[requests.PreparedRequest] = []

    def send(self, request, **kwargs):
        self.sent.append(request)
        r = requests.Response()
        r.status_code, r._content, r.url, r.request = 200, b"[]", request.url, request
        return r

    def close(self):
        pass


def test_the_request_carries_the_version_and_nothing_else(monkeypatch, tmp_path):
    """Privacy: no site, tenant, user or counts; no GitHub account from the person's .netrc; no cookie."""
    netrc = tmp_path / "netrc"
    netrc.write_text("machine api.github.com login someone password not-a-real-token\n", encoding="utf-8")
    monkeypatch.setenv("NETRC", str(netrc))
    wire = Wire()
    session = REAL_SESSION()
    session.mount("https://", wire)
    assert updates.check(http=session, now=T0) == {"checked": True, "problem": None}
    (sent,) = wire.sent
    assert dict(sent.headers) == {"User-Agent": "FOLIO-Exchange/0.2.0"}
    assert sent.method == "GET" and sent.body is None
    parts = urlparse(sent.url)
    assert (parts.scheme, parts.hostname, parts.path) == ("https", "api.github.com",
                                                          "/repos/ByYudkowskysTentacle/FOLIO-Exchange/releases")
    assert not parts.query and not parts.fragment and not parts.username


def test_the_faked_request_carries_only_the_version_too():
    gh = FakeGitHub(answer())
    updates.check(http=gh, now=T0)
    assert gh.calls[0]["headers"] == {"User-Agent": "FOLIO-Exchange/0.2.0"}


# ------------------------------------------------------------------ the switches
def test_off_in_the_environment_never_asks(monkeypatch):
    monkeypatch.setenv(updates.ENV, " OFF ")
    gh = FakeGitHub(answer(data=[release("v9.0.0")]))
    assert updates.mode() == "off" and not updates.due(T0)
    assert updates.check(http=gh, now=T0) == {"checked": False, "problem": None}
    assert updates.check(asked=True, http=gh, now=T0) == {"checked": False, "problem": None}
    assert gh.calls == [] and updates.look(http=gh, now=T0) is None
    assert updates.status()["mode"] == "off" and updates.status()["available"] is None


def test_check_false_in_updates_json_never_asks():
    updates._change(check=False, found={"any": {"version": "9.0.0"}, "stable": None})
    gh = FakeGitHub(answer())
    assert updates.mode() == "off"
    assert not updates.check(asked=True, http=gh, now=T0)["checked"] and gh.calls == []
    assert updates.status()["available"] is None and updates.to_tell() is None


def test_notify_checks_and_tells(monkeypatch):
    monkeypatch.setenv(updates.ENV, "notify")
    gh = FakeGitHub(answer(data=[release("v0.3.0")]))
    assert updates.mode() == "notify" and updates.look(http=gh, now=T0) == "0.3.0"
    assert updates.status()["mode"] == "notify"


def test_checking_automatically_can_be_turned_off_and_asked_for_by_hand():
    updates.set_choice("automatic", False)
    gh = FakeGitHub(answer(data=[release("v0.3.0")]))
    assert not updates.due(T0) and not updates.check(http=gh, now=T0)["checked"]
    assert updates.check(asked=True, http=gh, now=T0)["checked"] and updates.status()["automatic"] is False
    with pytest.raises(ValueError):
        updates.set_choice("check", False)                         # the IT switch is not one of the person's choices


def test_prereleases_count_by_default_until_1_0_and_the_choice_is_kept(monkeypatch):
    gh = FakeGitHub(answer(data=[release("v0.4.0", prerelease=True), release("v0.3.0")]))
    updates.check(http=gh, now=T0)
    assert updates.status()["prereleases"] and updates.status()["available"]["version"] == "0.4.0"
    updates.set_choice("prereleases", False)
    assert updates.status()["available"]["version"] == "0.3.0" and not updates.status()["available"]["prerelease"]
    updates.set_choice("prereleases", True)
    assert updates.status()["available"]["prerelease"] and len(gh.calls) == 1      # no new request for the choice
    (places.config_dir() / updates.FILE).unlink()
    monkeypatch.setattr(updates, "__version__", "1.2.0")
    assert not updates.status()["prereleases"]                     # from 1.0, only once someone ticks the box


def test_nothing_is_available_once_this_version_has_caught_up(monkeypatch):
    updates.check(http=FakeGitHub(answer(data=[release("v0.3.0")])), now=T0)
    assert updates.to_tell() == "0.3.0"
    monkeypatch.setattr(updates, "__version__", "0.3.0")
    assert updates.status()["available"] is None and updates.to_tell() is None


def test_each_new_version_is_told_once():
    gh = FakeGitHub(answer(data=[release("v0.3.0")]), answer(data=[release("v0.3.1"), release("v0.3.0")]))
    assert updates.look(http=gh, now=T0) == "0.3.0"
    updates.told("0.3.0")
    assert updates.look(http=gh, now=T0 + HOUR) is None
    assert updates.look(http=gh, now=T0 + 24 * HOUR) == "0.3.1"
    updates.told("0.3.1")
    assert updates.to_tell() is None


def test_a_choice_that_cannot_be_saved_is_raised(monkeypatch, tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setattr(places, "config_dir", lambda: blocker / "inside")
    with pytest.raises(OSError):
        updates.set_choice("prereleases", True)
    assert updates.check(http=FakeGitHub(answer()), now=T0) == {"checked": True, "problem": None}   # only logged


# ------------------------------------------------------------------ what is shown, and the key
def test_the_pip_command_names_the_repository_and_tag_never_the_bare_name():
    assert updates.pip_command("0.3.0") == ('pip install "folio-exchange[desktop] @ git+https://github.com/'
                                            'ByYudkowskysTentacle/FOLIO-Exchange.git@v0.3.0"')
    assert updates.pip_command("0.3.0", r"C:\FX\program\Scripts\python.exe").startswith(
        r'"C:\FX\program\Scripts\python.exe" -m pip install "folio-exchange[desktop] @ git+https://')


def test_the_release_page_is_built_here_not_taken_from_the_answer():
    gh = FakeGitHub(answer(data=[release("v0.3.0")]))                # its html_url points elsewhere
    updates.check(http=gh, now=T0)
    assert updates.status()["available"]["page"] == "https://github.com/ByYudkowskysTentacle/FOLIO-Exchange/releases/tag/v0.3.0"
    assert "evil.example" not in (places.config_dir() / updates.FILE).read_text(encoding="utf-8")


def test_long_release_notes_are_cut():
    gh = FakeGitHub(answer(data=[release("v0.3.0", body="x" * 50_000)]))
    updates.check(http=gh, now=T0)
    assert len(updates.status()["available"]["notes"]) == updates.MAX_NOTES


def test_the_release_key_is_unset_and_unused_until_u1():
    """U0 only tells. The public key's place exists; nothing reads it, and no key is in the repository."""
    assert updates.RELEASE_KEY is None
    source = Path(updates.__file__).read_text(encoding="utf-8")
    code = [line for line in source.splitlines() if "RELEASE_KEY" in line and not line.lstrip().startswith("#")]
    assert code == ["RELEASE_KEY: bytes | None = None"]
    assert "PRIVATE KEY" not in source


# ------------------------------------------------------------------ the scheduled task
def test_run_due_makes_no_update_request(site, folio, monkeypatch):
    """The scheduled task never checks for updates: it talks to FOLIO and the vendor only."""
    make_feed(site, schedule="daily 00:00")
    with History(site / "data" / "history.sqlite") as hist:          # known for a week: due, so the pass has work
        hist.set_meta("feed_seen:chq", (datetime.now(timezone.utc) - timedelta(days=7)).isoformat())
    def refuse(*args, **kwargs):
        raise AssertionError("run --due looked for updates")

    for name in ("check", "look", "fetch_releases", "_session"):
        monkeypatch.setattr(updates, name, refuse)
    asked: list[str] = []
    send = requests.Session.send

    def only_folio(self, request, **kwargs):
        asked.append(urlparse(request.url).hostname)
        if not folio.url.startswith(f"http://{asked[-1]}"):
            raise requests.ConnectionError(f"run --due asked {asked[-1]}")
        return send(self, request, **kwargs)

    monkeypatch.setattr(requests.Session, "send", only_folio)
    monkeypatch.setattr(requests, "get", refuse)
    monkeypatch.setattr(requests, "request", refuse)
    main(["-s", str(site), "run", "--due"])
    with History(site / "data" / "history.sqlite") as hist:
        assert hist.last_run("chq")["trigger"] == "schedule"         # the feed did run
    assert asked and set(asked) == {urlparse(folio.url).hostname}
    assert not (places.config_dir() / updates.FILE).exists()


def test_the_command_line_never_loads_the_update_check():
    code = "import sys, folio_exchange.cli, folio_exchange.runner; print('folio_exchange.updates' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         cwd=Path(__file__).parent)
    assert out.stdout.strip() == "False"
