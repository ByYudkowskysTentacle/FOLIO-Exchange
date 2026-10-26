"""End to end through the command line, against the mock FOLIO. Ported from folio-chq-extract's tests."""
import csv
import json

import pytest
from mock_folio import U
from pymarc import MARCReader
from sitekit import make_feed, recent

from folio_exchange import client as client_mod
from folio_exchange.cli import main


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)


def runs(site, feed="chq"):
    return sorted(p for p in (site / "out" / feed).iterdir() if p.is_dir() and p.name[:1].isdigit())


def report(run_dir):
    return json.loads((run_dir / "run-report.json").read_text(encoding="utf-8"))


def read_outputs(run_dir, ext="psv", delimiter="|"):
    with open(next(run_dir.glob("*.mrc")), "rb") as fh:
        records = list(MARCReader(fh, to_unicode=True, force_utf8=True))
    with open(next(run_dir.glob(f"*.{ext}")), newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh, delimiter=delimiter))
    return records, rows


def deliver_once(site, *extra):
    """First delivery is held; release it so later runs deliver straight away."""
    assert main(["-s", str(site), "run", "chq", *extra]) == 2
    assert main(["-s", str(site), "release", "chq", "--yes"]) == 0


def test_first_run_is_held_then_released(site):
    assert main(["-s", str(site), "run", "chq"]) == 2
    run = runs(site)[-1]
    rep = report(run)
    assert rep["status"] == "held" and "first delivery" in rep["held"]
    assert not (site / "vendor-drop").exists() or not any((site / "vendor-drop").iterdir())
    assert main(["-s", str(site), "release", "chq", "--yes"]) == 0
    sent = sorted(p.name for p in (site / "vendor-drop").iterdir())
    assert sent == sorted(rep["build"]["files"])
    assert report(run)["released_by"]
    # The second run goes straight out.
    assert main(["-s", str(site), "run", "chq"]) == 0
    assert report(runs(site)[-1])["status"] == "success"


def test_full_run_outputs(site, folio):
    deliver_once(site)
    run = runs(site)[-1]
    rep = report(run)
    records, rows = read_outputs(run)

    # Bibs: in1 (SRS) and in2 (generated). in3 suppressed, in4 only has an ONLINE item, in5 has no items.
    by_001 = {r["001"].data: r for r in records}
    assert set(by_001) == {"in00000000001", "in00000000002"}
    r1 = by_001["in00000000001"]
    assert r1["245"]["a"] == "Learning Python /"
    assert not r1.get_fields("949")                                 # old local 949 stripped; no items embedded
    r2 = by_001["in00000000002"]
    assert r2["245"]["a"] == "A FOLIO-only record"
    assert r2["100"]["a"] == "Doe, Jane"
    assert r2["020"]["a"] == "9781234567897"
    assert r2["050"]["a"] == "Z678.9 .D64 2019"
    assert r2["999"]["i"] == U(2)
    assert r2.leader[9] == "a"
    assert r1["999"]["i"] == U(1)                                   # SRS fixture carries a wrong $i; corrected

    bcs = {row["Barcode"]: row for row in rows}
    assert set(bcs) == {"B0001", "B0002", "B0003", "B0005", "None"}  # Withdrawn kept, no-barcode kept
    assert bcs["B0005"]["Status"] == "Withdrawn"
    assert {row["Bib record ID"] for row in rows} <= {r["999"]["i"] for r in records}

    b1 = bcs["B0001"]
    assert b1["Number of loans"] == "2" and b1["Number of renewals"] == "2"
    assert b1["Last_circ_date"] == recent(30).replace("T", " ").rstrip("Z")[:16] + b1["Last_circ_date"][16:]
    assert b1["Material_type"] == "book" and b1["Location"] == "Main Stacks [MAIN-STACKS]"
    b3 = bcs["B0003"]
    assert b3["Number of loans"] == "43"                            # 1 FOLIO loan + 42 legacy
    assert b3["Created_date"] == "1998-03-15 00:00:00"              # legacy note wins over migration date
    assert b3["Item_level_call_number"] == "DVD PN1997 .X1"
    assert b3["Location"] == "Science Library Stacks [SCI-STACKS]"

    b = rep["build"]
    assert b["items_without_barcode"] == 1
    assert b["marc_999_corrected"] == 1
    assert b["bibs_generated"] == 1
    assert b["items_skipped"]["location excluded"] == 1
    assert b["items_skipped"]["instance suppressed"] == 1
    assert rep["recipe"] == "chq-academic" and rep["recipe_version"] == "1.0.0"
    assert all(len(f["sha256"]) == 64 for f in rep["files"])


def test_loans_never_keep_the_borrower(site, folio):
    assert main(["-s", str(site), "sync"]) == 0
    import sqlite3

    db = sqlite3.connect(site / "data" / "cache.sqlite")
    cols = [r[1] for r in db.execute("PRAGMA table_info(loans)")]
    assert "user_id" not in cols and "userId" not in cols
    dump = "\n".join(db.iterdump())
    assert U(901) not in dump and U(902) not in dump and U(903) not in dump
    db.close()
    assert not any("/users" in r or "/circulation" in r for r in folio.requests)


def test_delta_picks_up_changes_and_deletions(site, folio):
    deliver_once(site)
    later = recent(0).replace("Z", ".000+00:00")
    it = next(i for i in folio.data["items"] if i["barcode"] == "B0001")
    it["status"] = {"name": "Missing", "date": later}
    it["metadata"]["updatedDate"] = later
    folio.data["items"] = [i for i in folio.data["items"] if i["barcode"] != "B0002"]
    assert main(["-s", str(site), "run", "chq", "--force"]) == 0
    rep = report(runs(site)[-1])
    assert rep["sync"]["mode"]["instances"] == "delta"
    assert rep["sync"]["mode"]["items"] == "full"                   # deletion detected -> full resync
    _, rows = read_outputs(runs(site)[-1])
    bcs = {r["Barcode"]: r for r in rows}
    assert bcs["B0001"]["Status"] == "Missing"
    assert "B0002" not in bcs


def test_safety_check_blocks_delivery_and_release_overrides(site, folio):
    deliver_once(site)
    folio.data["items"] = folio.data["items"][:1]
    assert main(["-s", str(site), "run", "chq"]) == 2
    rep = report(runs(site)[-1])
    assert rep["status"] == "blocked" and "fell from" in rep["error"]
    assert "release chq" in rep["next"]
    assert main(["-s", str(site), "release", "chq", "--yes"]) == 0
    assert report(runs(site)[-1])["status"] == "success"


def test_retries_and_relogin(site, folio):
    folio.fail_next = [503, 502]
    assert main(["-s", str(site), "sync"]) == 0
    folio.expire_tokens = True                                      # server rejects old token -> log in again
    assert main(["-s", str(site), "run", "chq", "--skip-sync", "--no-deliver"]) == 0
    assert main(["-s", str(site), "check"]) == 0


def test_bad_password(site, monkeypatch):
    monkeypatch.setenv("FOLIO_PASSWORD", "wrong")
    assert main(["-s", str(site), "run", "chq"]) == 1


def test_split_and_custom_columns(site):
    make_feed(site, outputs={
        "bibs": {"split_every": 1},
        "items": {"delimiter": "|", "date_format": "%d/%m/%Y", "extension": "csv", "empty_value": "",
                  "columns": [{"header": "BARCODE", "field": "barcode"}, {"header": "BIBID", "field": "instance_hrid"},
                              {"header": "CREATED", "field": "created_date"},
                              {"header": "Bib record ID", "field": "instance_id"},
                              {"header": "Status", "field": "status"}, {"header": "Material_type",
                                                                        "field": "material_type"}]}},
        filters={"include_items_without_barcode": False, "exclude_item_statuses": ["Withdrawn"],
                 "bibs_without_items": True, "exclude_location_codes": ["ONLINE"]})
    assert main(["-s", str(site), "run", "chq", "--no-deliver"]) == 0
    run = runs(site)[-1]
    assert len(list(run.glob("*.mrc"))) == 4                        # in1, in2, in4 (no eligible items), in5
    lines = next(run.glob("*.csv")).read_text(encoding="utf-8").splitlines()
    assert lines[0] == "BARCODE|BIBID|CREATED|Bib record ID|Status|Material_type"
    assert f"B0003|in00000000002|15/03/1998|{U(2)}|Available|dvd" in lines
    assert not any(line.startswith("|") for line in lines)          # no-barcode item dropped


def test_required_column_cannot_be_removed(site, capsys):
    make_feed(site, outputs={"items": {"columns": [{"header": "BARCODE", "field": "barcode"}]}})
    assert main(["-s", str(site), "run", "chq"]) == 1
    assert "requires the column 'Bib record ID'" in capsys.readouterr().err


def test_release_refuses_failed_and_edited_runs(site):
    assert main(["-s", str(site), "run", "chq", "--no-deliver"]) == 0
    run = runs(site)[-1]
    psv = next(run.glob("*.psv"))
    psv.write_text(psv.read_text(encoding="utf-8") + "extra\n", encoding="utf-8")
    assert main(["-s", str(site), "release", "chq", "--yes"]) == 1
    assert not (site / "vendor-drop").exists() or not any((site / "vendor-drop").iterdir())


def test_instance_switching_to_folio_source_drops_stale_marc(site, folio):
    deliver_once(site)
    inst = next(i for i in folio.data["instances"] if i["hrid"] == "in00000000001")
    inst["source"] = "FOLIO"
    inst["title"] = "Now FOLIO-sourced"
    inst["metadata"]["updatedDate"] = recent(0).replace("Z", ".000+00:00")
    assert main(["-s", str(site), "run", "chq"]) == 0
    records, _ = read_outputs(runs(site)[-1])
    r1 = next(r for r in records if r["001"].data == "in00000000001")
    assert r1["245"]["a"] == "Now FOLIO-sourced"


CHQ_ACADEMIC_COLUMNS = [
    ("Bib record ID", "instance_id"), ("Item record ID", "item_id"), ("Item record HRID", "item_hrid"),
    ("Item identifier", "item_identifier"), ("Barcode", "barcode"), ("Enumeration", "enumeration"),
    ("Chronology", "chronology"), ("Item_level_call_number", "call_number"), ("Volume", "volume"),
    ("Created_date", "created_date"), ("Status", "status"), ("Location", "location_name_code"),
    ("Material_type", "material_type"), ("Number_of_pieces", "number_of_pieces"), ("Copy_number", "copy_number"),
    ("Loan_name", "loan_type"), ("Last_circ_date", "last_circ_date"), ("Loan_return_date", "last_return_date"),
    ("Number of loans", "loans_total"), ("Number of renewals", "renewals_total"),
]


def test_chq_academic_psv_layout(site, folio, monkeypatch):
    """The bundled recipe's fixture test: the layout collectionHQ's implementation team gave, over SFTP."""
    make_feed(site, delivery={"transport": "sftp", "host": "sftp.example.org", "username": "lib",
                              "host_key_fingerprint": "SHA256:" + "A" * 43, "directory": None})
    sent = []
    monkeypatch.setattr("folio_exchange.delivery._deliver_sftp",
                        lambda files, _d, uploaded=None: sent.extend(p.name for p in files) or sent)
    deliver_once(site)
    run = runs(site)[-1]
    psv = next(run.glob("*.psv"))
    assert not list(run.glob("*.csv"))
    assert sorted(sent) == sorted([next(run.glob("*.mrc")).name, psv.name])
    assert psv.name.startswith("test_items_") and psv.name.endswith(".psv")

    raw = psv.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r\n" in raw          # UTF-8, no BOM, CRLF as in FOLIOHQ
    lines = psv.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "|".join(h for h, _ in CHQ_ACADEMIC_COLUMNS)
    with open(psv, newline="", encoding="utf-8") as fh:
        rows = {r["Barcode"]: r for r in csv.DictReader(fh, delimiter="|")}
    with open(next(run.glob("*.mrc")), "rb") as fh:
        records = list(MARCReader(fh, to_unicode=True, force_utf8=True))

    assert {r["999"]["i"] for r in records} >= {row["Bib record ID"] for row in rows.values()}
    assert not any(r.get_fields("949") for r in records)

    b1 = rows["B0001"]
    assert b1["Bib record ID"] == U(1) and b1["Item record ID"] == U(21) and b1["Item record HRID"] == "it21"
    assert b1["Item identifier"] == "LEGACY-21" and b1["Number_of_pieces"] == "2" and b1["Copy_number"] == "1"
    assert b1["Item_level_call_number"] == "QA76.73.P98 L88 2020"
    assert b1["Location"] == "Main Stacks [MAIN-STACKS]" and b1["Loan_name"] == "Can circulate"
    assert b1["Created_date"] == "2023-08-01 09:00:00"
    recent_loan = next(loan for loan in folio.data["loans"] if loan["id"] == U(42))
    assert b1["Last_circ_date"] == recent_loan["loanDate"].replace("T", " ").rstrip("Z")
    assert b1["Loan_return_date"] == recent_loan["returnDate"].replace("T", " ").rstrip("Z")
    assert b1["Number of loans"] == "2" and b1["Number of renewals"] == "2"
    assert b1["Enumeration"] == "None" and b1["Chronology"] == "None" and b1["Volume"] == "None"
    assert rows["None"]["Item identifier"] == "None"
    assert rows["B0002"]["Loan_return_date"] == "None" and rows["B0002"]["Volume"] == "v.2"


def _fake_keyring(monkeypatch, store: dict):
    import sys
    import types

    fake = types.SimpleNamespace(
        get_password=lambda s, u: store.get((s, u)),
        set_password=lambda s, u, v: store.__setitem__((s, u), v),
        get_keyring=lambda: types.SimpleNamespace(priority=5),
    )
    monkeypatch.setitem(sys.modules, "keyring", fake)
    return store


def test_folio_password_from_keyring(site, monkeypatch, capsys):
    monkeypatch.delenv("FOLIO_PASSWORD")
    _fake_keyring(monkeypatch, {("folio-exchange/folio", "fx_reader@diku"): "secret"})
    assert main(["-s", str(site), "check"]) == 0
    assert "FOLIO password: system keyring (folio-exchange/folio, fx_reader@diku)" in capsys.readouterr().out
    assert main(["-s", str(site), "run", "chq"]) == 2                # held, so the run itself worked


def test_folio_password_missing_everywhere(site, monkeypatch, caplog):
    monkeypatch.delenv("FOLIO_PASSWORD")
    _fake_keyring(monkeypatch, {("folio-exchange/sftp", "fx_reader@diku"): "not-this-one"})
    assert main(["-s", str(site), "check"]) == 1
    assert "secret set folio" in caplog.text


def test_secret_set_stores_in_keyring_without_echo(site, monkeypatch, capsys):
    store = _fake_keyring(monkeypatch, {})
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "s3cret-value")
    assert main(["-s", str(site), "secret", "set", "folio"]) == 0
    assert store[("folio-exchange/folio", "fx_reader@diku")] == "s3cret-value"
    out = capsys.readouterr()
    assert "s3cret-value" not in out.out + out.err
    log = (site / "logs" / "folio-exchange.log").read_text(encoding="utf-8")
    assert "s3cret-value" not in log


def test_feeds_history_and_schedule_commands(site, capsys):
    deliver_once(site)
    assert main(["-s", str(site), "feeds"]) == 0
    out = capsys.readouterr().out
    assert "chq-academic@1.0.0" in out and "Sundays at 02:15" in out
    assert main(["-s", str(site), "history"]) == 0
    assert main(["-s", str(site), "history", "--audit"]) == 0
    assert "released held run" in capsys.readouterr().out
    assert main(["-s", str(site), "schedule"]) == 0
    assert "run --due" in capsys.readouterr().out
    assert main(["-s", str(site), "feeds", "pause", "chq"]) == 0
    assert main(["-s", str(site), "run", "--all"]) == 0             # nothing enabled: nothing to run
    assert main(["-s", str(site), "feeds", "resume", "chq"]) == 0
    text = (site / "feeds" / "chq.yaml").read_text(encoding="utf-8")
    assert "enabled: true" in text


def test_recipes_and_fields_need_no_site(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["fields"]) == 0
    assert main(["recipes"]) == 0
    assert "chq-academic" in capsys.readouterr().out
    assert main(["recipes", "summary", "chq-academic"]) == 0
    out = capsys.readouterr().out
    assert "Bib record ID" in out and "999 ff $i" in out


def test_preview_writes_preview_files_only(site, capsys):
    assert main(["-s", str(site), "preview", "chq", "-n", "2"]) == 0
    out = capsys.readouterr().out
    assert "PREVIEW_" in out
    prev = sorted((site / "out" / "chq").glob("preview-*"))[-1]
    assert all(p.name.startswith(("PREVIEW_", "run-report")) for p in prev.iterdir())
    assert not (site / "vendor-drop").exists()
    assert not (site / "data" / "preview-cache.sqlite").exists()
