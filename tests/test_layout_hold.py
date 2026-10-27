"""SPEC-1: any change to the layout the vendor receives holds the next delivery, as a new recipe major does."""
import copy
import json
from pathlib import Path

import pytest
import yaml
from sitekit import make_feed

from folio_exchange import client as client_mod
from folio_exchange import runner
from folio_exchange.cli import main
from folio_exchange.config import BUNDLED_RECIPES, load_site
from folio_exchange.history import History
from folio_exchange.recipe import RecipeLibrary, layout_changes, load_feeds, recipe_digest

RECIPE = BUNDLED_RECIPES / "chq-academic" / "1.0.0.yaml"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)


def hist(site):
    return History(site / "data" / "history.sqlite")


def released(site):
    assert main(["-s", str(site), "run", "chq"]) == 2              # first delivery to the folder is held
    assert main(["-s", str(site), "release", "chq", "--yes"]) == 0


def recipe_columns() -> list[dict]:
    data = yaml.safe_load(RECIPE.read_text(encoding="utf-8"))
    return copy.deepcopy(next(o for o in data["outputs"] if o["name"] == "items")["columns"])


def renamed_columns(old: str, new: str) -> list[dict]:
    return [{**c, "header": new} if c["header"] == old else c for c in recipe_columns()]


def held_reason(site) -> str:
    with hist(site) as h:
        run = h.last_run("chq")
    assert run["status"] == "held"
    return run["report"]["held"]


def test_a_renamed_column_is_held_with_the_reason(site):
    released(site)
    make_feed(site, outputs={"items": {"columns": renamed_columns("Item record ID", "Item ID")}})
    assert main(["-s", str(site), "run", "chq"]) == 2
    reason = held_reason(site)
    assert "layout changed since the last delivery" in reason
    assert "items file: columns changed: 'Item record ID' renamed to 'Item ID'" in reason
    assert "first delivery" not in reason


def test_the_same_layout_is_delivered_and_recorded(site):
    released(site)
    assert main(["-s", str(site), "run", "chq"]) == 0
    with hist(site) as h:
        run = h.last_run("chq")
    assert run["status"] == "success" and run["delivered"]
    report = json.loads((Path(run["run_dir"]) / "run-report.json").read_text(encoding="utf-8"))
    assert report["recipe_sha256"] == recipe_digest(RecipeLibrary([BUNDLED_RECIPES]).latest("chq-academic"))
    items = report["layout"]["outputs"]["items"]
    assert items["delimiter"] == "|" and ["Item record ID", "item_id"] in items["columns"]
    assert report["layout"] == run["report"]["layout"]


def test_a_delivery_from_before_layouts_were_kept_does_not_hold(site):
    released(site)
    with hist(site) as h:                                           # make it look like an older version's row
        row = h.last_delivered("chq")
        old = {k: v for k, v in row["report"].items() if k not in ("layout", "recipe_sha256")}
        h.db.execute("UPDATE runs SET report=? WHERE id=?", (json.dumps(old), row["id"]))
        h.db.commit()
    make_feed(site, outputs={"items": {"delimiter": ","}})
    assert main(["-s", str(site), "run", "chq"]) == 0               # unknown is not a reason to hold
    with hist(site) as h:
        assert h.last_delivered("chq")["report"]["layout"]["outputs"]["items"]["delimiter"] == ","
    make_feed(site, outputs={"items": {"delimiter": ";"}})
    assert main(["-s", str(site), "run", "chq"]) == 2               # recorded this time, so the next change holds
    assert "delimiter changed from , to ;" in held_reason(site)


def test_release_sends_a_run_held_for_its_layout(site):
    released(site)
    make_feed(site, outputs={"items": {"columns": renamed_columns("Barcode", "Item barcode")}})
    assert main(["-s", str(site), "run", "chq"]) == 2
    s = load_site(site)
    feed = load_feeds(s)[0]
    seen = {}
    res = runner.release(s, feed, None, confirm=lambda summary: seen.update(summary) or True)
    assert res.status == "success"
    assert "'Barcode' renamed to 'Item barcode'" in seen["reason"]
    sent = sorted((site / "vendor-drop").glob("*.psv"))
    assert sent and "|Item barcode|" in sent[-1].read_text(encoding="utf-8").splitlines()[0]
    assert main(["-s", str(site), "run", "chq"]) == 0               # the new layout is now the baseline


@pytest.mark.parametrize("change, expected", [
    ({"delimiter": ","}, "items file: delimiter changed from | to ,"),
    ({"date_format": "%Y-%m-%d"}, "items file: date format changed from %Y-%m-%d %H:%M:%S to %Y-%m-%d"),
    ({"empty_value": ""}, "items file: empty value changed from None to nothing (empty)"),
    ({"header": False}, "items file: header row changed from on to off"),
    ({"extension": "txt"}, "items file: file extension changed from psv to txt"),
])
def test_format_changes_are_held(site, change, expected):
    released(site)
    make_feed(site, outputs={"items": change})
    assert main(["-s", str(site), "run", "chq"]) == 2
    assert expected in held_reason(site)


def test_marc_and_prefix_changes_are_held(site):
    released(site)
    make_feed(site, file_prefix="lib", outputs={"bibs": {"split_every": 1000}})
    assert main(["-s", str(site), "run", "chq"]) == 2
    reason = held_reason(site)
    assert "file prefix changed from test to lib" in reason
    assert "bibs file: records per file changed from 0 to 1000" in reason


def test_changes_that_do_not_touch_the_layout_are_not_held(site):
    released(site)
    make_feed(site, checks={"min_item_ratio": 0.5}, schedule="daily 03:00")
    assert main(["-s", str(site), "run", "chq"]) == 0


def test_column_change_descriptions():
    cols = [["A", "a"], ["B", "b"], ["C", "c"]]
    sig = {"outputs": {"items": {"columns": cols}}}

    def change(new):
        return layout_changes(sig, {"outputs": {"items": {"columns": new}}})

    assert change(cols) == []
    assert change([["A", "a"], ["C", "c"], ["B", "b"]]) == ["items file: columns changed: the order changed"]
    assert change([["A", "a"], ["C", "c"]]) == ["items file: columns changed: 'B' removed"]
    assert change([*cols, ["D", "d"]]) == ["items file: columns changed: 'D' added"]
    assert change([["A", "a"], ["B", "x"], ["C", "c"]]) == ["items file: columns changed: 'B' now holds x (was b)"]
    assert change([["Z", "a"], ["B", "b"], ["C", "c"]]) == ["items file: columns changed: 'A' renamed to 'Z'"]
    assert layout_changes({"outputs": {"items": {}}}, sig) == []              # an older signature: not compared
    assert layout_changes(sig, {"outputs": {}}) == ["items file no longer sent"]


# ------------------------------------------------------------------ site recipes
def site_copy(site, content: bytes):
    path = site / "recipes" / "chq-academic" / "1.0.0.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_a_site_copy_that_differs_from_the_bundled_recipe_is_reported(site):
    original = RECIPE.read_bytes()
    site_copy(site, b"\xef\xbb\xbf" + original.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert RecipeLibrary(load_site(site).recipe_dirs).shadow_warnings() == []      # same text, CRLF and a BOM
    path = site_copy(site, original.replace(b"header: Volume,", b"header: Volume number,"))
    warnings = RecipeLibrary(load_site(site).recipe_dirs).shadow_warnings()
    assert len(warnings) == 1
    assert str(path) in warnings[0] and "chq-academic@1.0.0" in warnings[0]
    assert "comes with FOLIO-Exchange" in warnings[0]
    lib = RecipeLibrary(load_site(site).recipe_dirs)
    assert lib.resolve("chq-academic@^1.0").path == path                          # it is still used
    assert recipe_digest(lib.resolve("chq-academic@^1.0")) != recipe_digest(
        RecipeLibrary([BUNDLED_RECIPES]).resolve("chq-academic@^1.0"))


def test_an_edited_site_copy_is_held_and_audited(site):
    site_copy(site, RECIPE.read_bytes())
    released(site)
    site_copy(site, RECIPE.read_bytes().replace(b"header: Volume,", b"header: Volume number,"))
    assert main(["-s", str(site), "run", "chq"]) == 2
    assert "'Volume' renamed to 'Volume number'" in held_reason(site)
    with hist(site) as h:
        entry = next(a for a in h.audit_log() if a["action"] == "settings changed")
    assert entry["detail"]["file"] == "recipes/chq-academic/1.0.0.yaml" and entry["feed"] is None
    assert entry["detail"]["changes"]["outputs[1].columns[8].header"] == {"from": "Volume", "to": "Volume number"}


def test_recipe_digest_ignores_line_endings(tmp_path):
    lf = RECIPE.read_bytes().replace(b"\r\n", b"\n")
    for name, content in (("a", lf), ("b", b"\xef\xbb\xbf" + lf.replace(b"\n", b"\r\n"))):
        (tmp_path / name / "chq-academic").mkdir(parents=True)
        (tmp_path / name / "chq-academic" / "1.0.0.yaml").write_bytes(content)
    a, b = (RecipeLibrary([tmp_path / n]).latest("chq-academic") for n in ("a", "b"))
    assert recipe_digest(a) == recipe_digest(b) and len(recipe_digest(a)) == 64


# ------------------------------------------------------------------ the time zone and header-less columns
def test_a_new_time_zone_is_held_for_delimited_and_embedded_marc_dates(site):
    make_feed(site, outputs={"bibs": {"embed_items": True}})
    released(site)
    make_feed(site, outputs={"bibs": {"embed_items": True}}, options={"timezone": "America/Detroit"})
    assert main(["-s", str(site), "run", "chq"]) == 2
    reason = held_reason(site)
    assert "items file: time zone changed from UTC to America/Detroit" in reason
    assert "bibs file: time zone changed from UTC to America/Detroit" in reason


def test_a_new_time_zone_does_not_touch_marc_without_dates(site):
    released(site)                                                  # bibs carry no items, so no dates
    make_feed(site, options={"timezone": "America/Detroit"})
    assert main(["-s", str(site), "run", "chq"]) == 2
    reason = held_reason(site)
    assert "items file: time zone changed from UTC to America/Detroit" in reason and "bibs file" not in reason


def test_without_a_header_row_a_renamed_header_is_not_held(site):
    make_feed(site, outputs={"items": {"header": False}})
    released(site)
    make_feed(site, outputs={"items": {"header": False, "columns": renamed_columns("Item record ID", "Item ID")}})
    assert main(["-s", str(site), "run", "chq"]) == 0               # nothing the vendor receives changed
    moved = recipe_columns()
    moved[1], moved[2] = moved[2], moved[1]
    make_feed(site, outputs={"items": {"header": False, "columns": moved}})
    assert main(["-s", str(site), "run", "chq"]) == 2               # the order of the fields is still the layout
    assert "items file: columns changed: the order changed" in held_reason(site)


def test_switching_the_header_row_off_names_only_that(site):
    released(site)
    make_feed(site, outputs={"items": {"header": False}})
    assert main(["-s", str(site), "run", "chq"]) == 2
    reason = held_reason(site)
    assert "items file: header row changed from on to off" in reason and "columns changed" not in reason


def test_header_less_column_descriptions():
    before = {"outputs": {"items": {"header": False, "columns": [["A", "a"], ["B", "b"]]}}}   # recorded with headers
    same = {"outputs": {"items": {"header": False, "columns": [[None, "a"], [None, "b"]]}}}
    assert layout_changes(before, same) == []
    changed = {"outputs": {"items": {"header": False, "columns": [[None, "a"], [None, "c"]]}}}
    assert layout_changes(same, changed) == ["items file: columns changed: 'b' removed, 'c' added"]
    assert layout_changes(same, {"outputs": {"items": {"header": False, "columns": [[None, "a"]]}}}) == [
        "items file: columns changed: 'b' removed"]


def test_a_layout_recorded_before_time_zones_were_kept_does_not_hold():
    old = {"outputs": {"items": {"date_format": "%Y-%m-%d"}}}
    assert layout_changes(old, {"outputs": {"items": {"date_format": "%Y-%m-%d", "timezone": "Europe/London"}}}) == []
