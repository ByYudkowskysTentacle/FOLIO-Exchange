"""The local page (`folio-exchange ui`): its safety rules, and the run, review and send flow over its API."""
import json
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

import pytest
from sitekit import FOLIO_TEST_PASSWORD, make_feed

from folio_exchange import client as client_mod
from folio_exchange import ui
from folio_exchange.config import load_site
from folio_exchange.errors import ConfigError
from folio_exchange.history import History


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
    # no_sleep patches time.sleep for the whole process, so wait on the clock with an Event instead
    deadline = time.monotonic() + 60
    pause = threading.Event()
    while time.monotonic() < deadline:
        status, j, _ = call(app, f"/api/jobs/{job['id']}")
        assert status == 200
        if j["status"] != "running":
            return j
        pause.wait(0.1)
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


def test_site_warnings_carry_a_code_for_the_page(server):
    data = call(server, "/api/site")[1]
    codes = {w["code"] for w in data["warnings"]}
    assert "no-alerts" in codes                                   # the test site has no alert channel
    assert all({"level", "code", "text", "feed"} <= set(w) for w in data["warnings"])
    assert "newer_recipe" in data["feeds"][0]


def test_open_folder_opens_only_a_run_folder_inside_out(server, site, monkeypatch):
    opened = []
    monkeypatch.setattr(ui, "open_folder", opened.append)
    run = wait(server, call(server, "/api/feeds/chq/preview", {"instances": 2})[1])["result"]["run"]
    assert call(server, f"/api/runs/{run['id']}/open-folder", {}, token=False)[0] == 403
    assert not opened
    status, body, _ = call(server, f"/api/runs/{run['id']}/open-folder", {})
    assert status == 200 and body == {"opened": run["folder"]}
    assert len(opened) == 1 and opened[0].name == run["folder"] and opened[0].is_dir()
    assert call(server, "/api/runs/999/open-folder", {})[0] == 404

    outside = site.parent / "elsewhere"
    outside.mkdir()
    loaded = load_site(site)
    with History(loaded.history_path) as hist:
        hist.db.execute("UPDATE runs SET run_dir=? WHERE id=?", (str(outside), run["id"]))
        hist.db.commit()
    status, body, _ = call(server, f"/api/runs/{run['id']}/open-folder", {})
    assert status == 400 and "outside" in body["error"]
    with pytest.raises(ConfigError):
        ui.run_folder(loaded, {"run_dir": str(loaded.out_dir)})            # the output folder itself
    a_file = loaded.out_dir / "chq" / "note.txt"
    a_file.write_text("x", encoding="utf-8")
    with pytest.raises(ConfigError):
        ui.run_folder(loaded, {"run_dir": str(a_file)})                   # never a file: it could be a program
    assert len(opened) == 1


def test_open_folder_never_goes_through_a_shell(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(ui.sys, "platform", "linux")
    monkeypatch.setattr(ui.subprocess, "Popen", lambda args, **kw: calls.append((args, kw)))
    ui.open_folder(tmp_path / "a folder; rm -rf ~")
    (args, kw), = calls
    assert args == ["xdg-open", str(tmp_path / "a folder; rm -rf ~")] and not kw.get("shell")


# ------------------------------------------------------------------ the page's own files
WEB = ui.WEB


def _tokens(css: str) -> tuple[dict, dict]:
    light_block = re.search(r":root\s*\{(.*?)\}", css, re.S).group(1)
    dark_block = re.search(r"prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{(.*?)\}", css, re.S).group(1)
    parse = lambda block: dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})", block))  # noqa: E731
    light = parse(light_block)
    return light, {**light, **parse(dark_block)}


def _contrast(a: str, b: str) -> float:
    def lum(hex_: str) -> float:
        c = [int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


TEXT_PAIRS = [
    ("--accent-text", "--accent-strong"),                       # the Run button
    ("--accent-ink", "--panel-2"), ("--accent-ink", "--panel"),  # the recipe label, required marks
    ("--muted", "--panel"), ("--muted", "--panel-2"), ("--muted", "--neutral-bg"),
    ("--neutral", "--neutral-bg"), ("--ok", "--ok-bg"), ("--held", "--held-bg"), ("--blocked", "--blocked-bg"),
    ("--failed", "--failed-bg"), ("--built", "--built-bg"),      # the small badges
    ("--failed", "--panel"), ("--text", "--panel"), ("--accent-text", "--accent-hover"),
]


def test_text_colours_meet_wcag_aa_in_both_schemes():
    light, dark = _tokens((WEB / "app.css").read_text(encoding="utf-8"))
    for name, scheme in (("light", light), ("dark", dark)):
        low = [(fg, bg, round(_contrast(scheme[fg], scheme[bg]), 2)) for fg, bg in TEXT_PAIRS
               if _contrast(scheme[fg], scheme[bg]) < 4.5]
        assert not low, f"{name} scheme below 4.5:1: {low}"


def test_the_page_speaks_its_own_language_and_works_by_keyboard():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    html = (WEB / "index.html").read_text(encoding="utf-8")
    # no command lines for things the page can do itself, and no run.next (the command line's next step)
    assert not re.search(r"folio-exchange (release|upgrade|resume|check|run)\b", js)
    assert "run.next" not in js and "last.next" not in js
    # tabs, the Run menu and history rows are reachable and announced
    for needle in ('role: "tab"', '"aria-controls"', 'role: "tabpanel"', '"aria-expanded"', 'role: "menuitem"',
                   '"ArrowDown"', 'Home: 0', '"Escape"', 'class: "row-open"', '"visibilitychange"'):
        assert needle in js, needle
    # the strict CSP: no inline scripts or styles
    assert "<script>" not in html and "style=" not in html and ".style." not in js
    css = (WEB / "app.css").read_text(encoding="utf-8")
    narrow = css[css.index("@media (max-width: 760px)"):]
    assert ".main, .split-v { display: block; overflow: visible; }" in narrow


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js to run the page's script")
def test_page_words_keeps_commands_the_page_cannot_stand_in_for():
    # Regression: every sentence naming any folio-exchange command was dropped, so a missing SFTP password
    # lost its only remedy (`secret set`), which the page has no button for.
    js = (WEB / "app.js").read_text(encoding="utf-8")
    start = js.index("const PAGE_HAS_IT")
    end = js.index("\n}\n", js.index("function pageWords")) + 3
    cases = [
        "No SFTP password found for a@b. Store it with `folio-exchange secret set chq`.",
        "Item count fell. Files were built but not delivered. Then send them with `folio-exchange release chq`.",
        "Login failed. Run `folio-exchange check` to test the connection.",
        "Not tested. Run `folio-exchange check --alerts` after adding one.",
    ]
    script = js[start:end] + f"\nconsole.log(JSON.stringify({json.dumps(cases)}.map(pageWords)));"
    out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    assert out[0] == cases[0]
    assert out[1] == "Item count fell. Files were built but not delivered."
    assert out[2] == "Login failed."
    assert out[3] == cases[3]
