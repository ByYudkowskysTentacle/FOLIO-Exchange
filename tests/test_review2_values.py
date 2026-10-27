"""Second review round: legacy note values are read strictly and counted when unusable
(FIELD-legacy-note-parsing), and out-of-range dates never crash a build (FIELD-legacy-date-overflow-crash)."""
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import jsonschema
import pytest
from sitekit import make_feed

from folio_exchange import client as client_mod
from folio_exchange.build import BuildReport, legacy_warnings
from folio_exchange.cli import main
from folio_exchange.config import load_site
from folio_exchange.dates import fmt, legacy, legacy_format_problem, norm, parse
from folio_exchange.errors import ConfigError
from folio_exchange.fields import Ref, RowBuilder, read_count
from folio_exchange.recipe import RecipeLibrary, load_feed

SCHEMAS = Path(__file__).parent.parent / "src" / "folio_exchange" / "schemas"
DETROIT = ZoneInfo("America/Detroit")
SYDNEY = ZoneInfo("Australia/Sydney")
NOW = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)


def leg(value, tz=DETROIT, date_format=None, now=NOW):
    return legacy(value, tz, date_format, now)


# ------------------------------------------------------------------ dates
def test_two_digit_years_fall_in_the_past():
    assert leg("06-12-68") == "1968-06-12T04:00:00"
    assert leg("11-03-72") == "1972-11-03T05:00:00"
    assert leg("3/15/98") == "1998-03-15T05:00:00"


@pytest.mark.parametrize("value", ["1998", "March 1998", "1998-03"])
def test_partial_dates_are_rejected_not_filled_from_today(value):
    assert leg(value) is None
    assert leg(value, now=datetime(2026, 10, 28, tzinfo=timezone.utc)) is None


@pytest.mark.parametrize("value", ["2205-03-15", "1799-12-31", "0001-01-01", "01/01/0001", "2026-10-29"])
def test_implausible_dates_are_rejected(value):
    assert leg(value) is None


def test_today_and_tomorrow_are_accepted():
    assert leg("2026-10-27") == "2026-10-27T04:00:00"
    assert leg("2026-10-28") == "2026-10-28T04:00:00"


def test_a_label_before_a_date_is_ignored_but_trailing_text_is_not():
    assert leg("Created: 1998-03-15") == "1998-03-15T05:00:00"
    assert leg("Last checkout: 3/15/1998") == "1998-03-15T05:00:00"
    assert leg("1998-03-15 (Sierra)") is None


@pytest.mark.parametrize("value", ["1998-03-15", "3/15/1998", "1998-03-15T10:00:00Z", "1998-03-15T10:00:00-05:00",
                                   "1998-03-15 10:30:00", "March 15, 1998"])
def test_clean_values_read_exactly_as_before(value):
    assert leg(value, now=None) == norm(value, DETROIT) is not None


def test_date_format_reads_day_first_dates():
    assert leg("03/04/1998") == "1998-03-04T05:00:00"                       # month first without a pattern
    assert leg("03/04/1998", date_format="%d/%m/%Y") == "1998-04-03T05:00:00"
    assert leg("1998-04-03", date_format="%d/%m/%Y") == "1998-04-03T05:00:00"  # ISO still accepted
    assert leg("April 3 1998", date_format="%d/%m/%Y") is None              # no free text with a pattern
    assert leg("03/04/68", date_format="%d/%m/%y") == "1968-04-03T05:00:00"


def test_date_format_is_checked():
    assert legacy_format_problem("%d/%m/%Y") is None
    assert legacy_format_problem("%d.%m.%y") is None
    assert "no day" in legacy_format_problem("%m/%Y")
    assert "no day or month" in legacy_format_problem("%Y")
    assert legacy_format_problem(12)


def test_zone_conversion_out_of_range_returns_none():
    assert parse("0001-01-01", SYDNEY) is None
    assert parse("0001-01-01", ZoneInfo("Europe/Berlin")) is None
    assert parse("9999-12-31 23:00:00", DETROIT) is None
    assert norm("0001-01-01", SYDNEY) is None
    assert leg("0001-01-01", SYDNEY) is None
    assert fmt("0001-01-01T00:00:00", "%Y-%m-%d", DETROIT) == ""
    assert fmt("9999-12-31T23:00:00", "%Y-%m-%d", SYDNEY) == ""
    assert fmt("2024-03-02T04:30:00Z", "%Y-%m-%d", DETROIT) == "2024-03-01"


# ------------------------------------------------------------------ counts
@pytest.mark.parametrize("text, count", [("12", 12), ("Checkouts: 12", 12), ("1 234", 1234), ("1,234", 1234),
                                         ("Checkouts: 1,234", 1234), ("12.0", 12), ("0", 0)])
def test_counts(text, count):
    assert read_count(text) == count


@pytest.mark.parametrize("text", ["12 (since 2003)", "1.234", "-3", "12 checkouts", "twelve", "12, 13"])
def test_unusable_counts(text):
    assert read_count(text) is None


# ------------------------------------------------------------------ the row builder
NT_CREATED, NT_COUNT, NT_LAST = "nt-created", "nt-count", "nt-last"
LEGACY = {"created_date_note_type": "Old created", "circ_count_note_type": "Old count",
          "last_circ_date_note_type": "Old last"}


def builder(date_format=None, tz="America/Detroit"):
    tables = {k: {} for k in Ref.__dataclass_fields__}
    tables["item_note_types"] = {NT_CREATED: {"name": "Old created"}, NT_COUNT: {"name": "Old count"},
                                 NT_LAST: {"name": "Old last"}}
    return RowBuilder(Ref(**tables), {"timezone": tz}, {**LEGACY, "date_format": date_format})


def row(rb, n, created=None, count=None, last=None, loans=None):
    notes = [{"itemNoteTypeId": t, "note": v} for t, v in ((NT_CREATED, created), (NT_COUNT, count),
                                                           (NT_LAST, last)) if v is not None]
    item = {"id": f"id{n}", "hrid": f"it{n}", "notes": notes,
            "metadata": {"createdDate": "2023-08-01T09:00:00.000+00:00"}}
    return rb.build(item, {}, {}, loans or {})


def test_scenario_values_in_rows():
    rb = builder()
    assert row(rb, 1, created="06-12-68")["created_date"] == "1968-06-12T04:00:00"
    r = row(rb, 2, last="11-03-72", loans={"last_loan": "2026-09-01T14:00:00", "total": 3})
    assert r["last_checkout_date"] == "2026-09-01T14:00:00"                 # FOLIO's checkout is later
    assert row(rb, 3, last="2205-03-15", loans={"last_loan": "2026-09-01T14:00:00"})["last_checkout_date"] == \
        "2026-09-01T14:00:00"
    r = row(rb, 4, count="Checkouts: 12", loans={"total": 3})
    assert r["loans_total"] == 15 and r["legacy_circ_count"] == 12
    assert row(rb, 5, created="Created: 1998-03-15")["created_date"] == "1998-03-15T05:00:00"
    assert rb.unparsed == {"last_circ_date_note_type": {"count": 1, "examples": ["it3"]}}    # only 2205


def test_unusable_notes_are_counted_per_setting_with_examples():
    rb = builder()
    r = row(rb, 1, created="1998", count="12 (since 2003)", loans={"total": 3})
    assert r["created_date"] == "2023-08-01T09:00:00"                       # FOLIO's record-creation date
    assert r["loans_total"] == 3 and r["legacy_circ_count"] == 0
    for n in range(2, 9):
        row(rb, n, created="March 1998", last="2205-03-15")
    row(rb, 9, created="1998-03-15", count="7")                            # clean: not counted
    assert rb.unparsed["created_date_note_type"] == {"count": 8, "examples": ["it1", "it2", "it3", "it4", "it5"]}
    assert rb.unparsed["last_circ_date_note_type"] == {"count": 7, "examples": ["it2", "it3", "it4", "it5", "it6"]}
    assert rb.unparsed["circ_count_note_type"] == {"count": 1, "examples": ["it1"]}


def test_year_one_note_in_an_eastern_zone_is_counted_not_a_crash():
    rb = builder(tz="Australia/Sydney")
    r = row(rb, 1, created="0001-01-01", last="01/01/0001")
    assert r["created_date"] == "2023-08-01T09:00:00" and r["last_checkout_date"] is None
    assert rb.unparsed["created_date_note_type"]["count"] == 1
    assert rb.unparsed["last_circ_date_note_type"]["count"] == 1


def test_late_9999_note_in_a_western_zone_is_counted_not_a_crash():
    rb = builder()
    assert row(rb, 1, created="9999-12-31 23:00:00")["created_date"] == "2023-08-01T09:00:00"
    assert rb.unparsed["created_date_note_type"]["count"] == 1


def test_builder_uses_the_date_format():
    rb = builder(date_format="%d/%m/%Y", tz="Europe/London")
    assert row(rb, 1, created="03/04/1998")["created_date"] == "1998-04-02T23:00:00"   # 3 April, BST
    assert rb.unparsed == {}


def test_warning_text_names_the_setting_and_examples():
    rep = BuildReport(legacy_unparsed={"created_date_note_type": {"count": 1200, "examples": ["it1", "it2"]},
                                       "circ_count_note_type": {"count": 3, "examples": ["it9"]}})
    created, count = legacy_warnings(rep, {**LEGACY, "date_format": "%d/%m/%Y"})
    assert created.startswith("1,200 'Old created' notes (legacy.created_date_note_type)")
    assert "it1, it2" in created and "%d/%m/%Y" in created and "record-creation date" in created
    assert "'Old count'" in count and "it9" in count and "FOLIO's loans only" in count
    assert legacy_warnings(BuildReport(), LEGACY) == []


# ------------------------------------------------------------------ feed settings
def test_feed_date_format_setting(site):
    lib = RecipeLibrary(load_site(site).recipe_dirs)
    make_feed(site, legacy={"date_format": "%d/%m/%Y"})
    feed = load_feed(site / "feeds" / "chq.yaml", lib, site)
    assert feed.legacy["date_format"] == "%d/%m/%Y"
    raw = {"uses": "chq-academic@^1.0", "legacy": {"created_date_note_type": "x", "date_format": "%d/%m/%Y"}}
    jsonschema.validate(raw, json.loads((SCHEMAS / "feed.schema.json").read_text(encoding="utf-8")))
    make_feed(site, legacy={"date_format": "%m/%Y"})
    with pytest.raises(ConfigError, match=r"legacy\.date_format.*no day"):
        load_feed(site / "feeds" / "chq.yaml", lib, site)


# ------------------------------------------------------------------ end to end
def test_unusable_notes_reach_the_run_report_and_preview(site, folio, capsys):
    types = {t["name"]: t["id"] for t in folio.data["itemNoteTypes"]}
    created, count = types["Legacy created date"], types["Legacy circ count"]
    items = {i["hrid"]: i for i in folio.data["items"]}
    items["it21"]["notes"] = [{"itemNoteTypeId": created, "note": "1998"},
                              {"itemNoteTypeId": count, "note": "Checkouts: 12"}]
    items["it22"]["notes"] = [{"itemNoteTypeId": created, "note": "0001-01-01"},
                              {"itemNoteTypeId": count, "note": "12 (since 2003)"}]
    make_feed(site, options={"timezone": "Australia/Sydney"})

    assert main(["-s", str(site), "run", "chq"]) == 2                      # first delivery held, not crashed
    run = sorted(p for p in (site / "out" / "chq").iterdir() if p.is_dir() and p.name[:1].isdigit())[-1]
    rep = json.loads((run / "run-report.json").read_text(encoding="utf-8"))
    assert rep["status"] == "held"
    assert rep["build"]["legacy_unparsed"] == {
        "created_date_note_type": {"count": 2, "examples": ["it21", "it22"]},
        "circ_count_note_type": {"count": 1, "examples": ["it22"]},
    }
    warnings = rep["warnings"]
    assert any("legacy.created_date_note_type" in w and "it21, it22" in w for w in warnings)
    assert any("legacy.circ_count_note_type" in w and "it22" in w for w in warnings)
    with open(next(run.glob("*.psv")), newline="", encoding="utf-8") as fh:
        rows = {r["Barcode"]: r for r in csv.DictReader(fh, delimiter="|")}
    assert rows["B0001"]["Number of loans"] == "14"                         # 2 FOLIO loans + 'Checkouts: 12'
    assert rows["B0002"]["Created_date"].startswith("2023-08-01")           # FOLIO's date, not year 1
    assert rows["B0003"]["Created_date"] == "1998-03-15 00:00:00"           # clean values unchanged

    capsys.readouterr()
    assert main(["-s", str(site), "preview", "chq", "-n", "5"]) == 0
    out = capsys.readouterr().out
    assert "WARNING" in out and "legacy.created_date_note_type" in out and "legacy.circ_count_note_type" in out
