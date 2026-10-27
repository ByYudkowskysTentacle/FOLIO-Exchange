"""The local page (`folio-exchange ui`): its safety rules, and the run, review and send flow over its API."""
import json
import threading
import time
import urllib.error
import urllib.request

import pytest
from sitekit import FOLIO_TEST_PASSWORD, make_feed

from folio_exchange import client as client_mod
from folio_exchange import ui


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)


@pytest.fixture
def server(site):
    httpd, app = ui.start_in_thread(site)
    yield app
    httpd.shutdown()
    httpd.server_close()


def call(app, path, body=None, *, token=True, host=None, origin=None):
    url = f"http://127.0.0.1:{app.port}{path}"
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET")
    if body is not None:
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("X-FX-Token", app.token if token is True else token)
    if host:
        req.add_header("Host", host)
    if origin:
        req.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if r.headers.get_content_type() == "application/json" else raw), r.headers
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, json.loads(raw) if raw.startswith(b"{") else raw, e.headers


def wait(app, job):
    for _ in range(300):
        status, j, _ = call(app, f"/api/jobs/{job['id']}")
        assert status == 200
        if j["status"] != "running":
            return j
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def test_page_carries_the_token_and_strict_headers(server):
    status, html, headers = call(server, "/")
    assert status == 200 and server.token.encode() in html
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert headers["X-Frame-Options"] == "DENY"
    assert call(server, "/static/app.js")[0] == 200
    assert call(server, "/static/../ui.py")[0] == 404
    assert call(server, "/static/index.html")[0] == 404


def test_requests_must_name_this_computer_and_actions_need_the_token(server):
    assert call(server, "/api/site", host="evil.example:80")[0] == 403          # DNS rebinding
    assert call(server, "/api/check", {}, token=False)[0] == 403
    assert call(server, "/api/check", {}, token="wrong")[0] == 403
    assert call(server, "/api/check", {}, origin="https://evil.example")[0] == 403


def test_site_summary_never_includes_the_password(server, site):
    status, data, _ = call(server, "/api/site")
    assert status == 200
    assert [f["name"] for f in data["feeds"]] == ["chq"]
    assert data["folio"]["password"] == "environment (FOLIO_PASSWORD)"
    status, raw, _ = call(server, "/api/feeds/chq")
    assert status == 200
    for body in (json.dumps(data), json.dumps(raw)):
        assert f'"{FOLIO_TEST_PASSWORD}"' not in body


def test_run_review_and_send(server, site):
    status, job, _ = call(server, "/api/feeds/chq/run", {})
    assert status == 200 and job["status"] == "running"
    done = wait(server, job)
    assert done["status"] == "done" and done["result"]["status"] == "held"
    run = done["result"]["run"]
    assert run["status"] == "held" and run["items"] == 5

    feed = call(server, "/api/feeds/chq")[1]
    assert feed["waiting"]["id"] == run["id"]
    assert feed["layout"][1]["columns"][0]["header"] == "Bib record ID"

    rows = call(server, f"/api/runs/{run['id']}/rows")[1]
    assert rows["header"][0] == "Bib record ID" and rows["total"] == 5

    summary = call(server, "/api/feeds/chq/release-preview", {})[1]
    assert summary["reason"].startswith("first delivery") and not (site / "vendor-drop").exists()

    status, job, _ = call(server, "/api/feeds/chq/release", {"run_dir": summary["run_dir"]})
    done = wait(server, job)
    assert done["result"]["status"] == "success"
    assert sorted(p.name for p in (site / "vendor-drop").iterdir()) == sorted(f["name"] for f in run["files"])
    assert call(server, "/api/feeds/chq")[1]["waiting"] is None


def test_preview_and_check(server):
    done = wait(server, call(server, "/api/feeds/chq/preview", {"instances": 2})[1])
    assert done["result"]["status"] == "preview" and done["result"]["run"]["items"] > 0
    assert call(server, "/api/feeds/chq")[1]["last_run"] is None              # previews are not runs
    check = wait(server, call(server, "/api/check", {})[1])["result"]
    assert check["logged_in"] and check["ok"] and not check["denied"]
    assert {e["name"] for e in check["endpoints"]} >= {"instances", "items", "loans", "marc", "locations"}


def test_one_action_at_a_time(server, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(ui, "check_connection", lambda root: release.wait(10) and {"ok": True})
    first = call(server, "/api/check", {})[1]
    status, err, _ = call(server, "/api/feeds/chq/run", {})
    assert status == 409 and "still running" in err["error"]
    release.set()
    assert wait(server, first)["status"] == "done"


def test_pause_and_resume(server, site):
    assert call(server, "/api/feeds/chq/pause", {})[1] == {"name": "chq", "enabled": False}
    assert "enabled: false" in (site / "feeds" / "chq.yaml").read_text(encoding="utf-8")
    assert call(server, "/api/site")[1]["feeds"][0]["enabled"] is False
    call(server, "/api/feeds/chq/resume", {})
    audit = call(server, "/api/audit")[1]
    assert {"feed paused", "feed resumed"} <= {a["action"] for a in audit}


def test_problem_feeds_and_unknown_things(server, site):
    make_feed(site, name="broken", uses="chq-academic")
    data = call(server, "/api/site")[1]
    assert "broken" in data["problems"] and "no version range" in data["problems"]["broken"]
    assert call(server, "/api/feeds/broken")[0] == 400
    assert call(server, "/api/feeds/nope")[0] == 400
    assert call(server, "/api/runs/999")[0] == 404
    assert call(server, "/api/feeds/chq/delete", {})[0] == 404


def test_bad_numbers_are_a_bad_request(server):
    assert call(server, "/api/feeds/chq/runs?limit=abc")[0] == 400


def test_the_page_can_find_a_running_job_after_a_reload(server, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(ui, "check_connection", lambda root: release.wait(10) and {"ok": True})
    job = call(server, "/api/check", {})[1]
    site = call(server, "/api/site")[1]
    assert site["job"]["id"] == job["id"] and site["job"]["status"] == "running"
    release.set()
    wait(server, job)
    assert call(server, "/api/site")[1]["job"] is None


def test_ctrl_c_waits_for_a_running_job(server, monkeypatch, capsys):
    release = threading.Event()
    monkeypatch.setattr(ui, "check_connection", lambda root: release.wait(10) and {"ok": True})
    call(server, "/api/check", {})
    threading.Timer(0.3, release.set).start()
    ui._finish_running_job(server)
    err = capsys.readouterr().err
    assert "still running; waiting for it to finish" in err and "finished: done" in err
