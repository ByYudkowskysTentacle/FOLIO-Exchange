"""Recipes: versions and ranges, validation messages, the library, and feed overlay rules."""
import copy
from pathlib import Path

import pytest
import yaml
from sitekit import feed_data, write_yaml

from folio_exchange.config import BUNDLED_RECIPES
from folio_exchange.errors import ConfigError, RecipeError
from folio_exchange.recipe import (
    RecipeLibrary,
    load_feed,
    load_recipe_file,
    parse_uses,
    validate_recipe,
    version_matches,
)

CHQ = BUNDLED_RECIPES / "chq-academic" / "1.0.0.yaml"


def chq() -> dict:
    return yaml.safe_load(CHQ.read_text(encoding="utf-8"))


def write_recipe(lib: Path, data: dict) -> Path:
    return write_yaml(lib / data["recipe"] / f"{data['version']}.yaml", data)


@pytest.mark.parametrize("version,spec,ok", [
    ((1, 2, 0), "1.2.0", True), ((1, 2, 1), "1.2.0", False),
    ((1, 2, 0), "^1.2", True), ((1, 9, 3), "^1.2", True), ((2, 0, 0), "^1.2", False), ((1, 1, 9), "^1.2", False),
    ((0, 3, 5), "^0.3", True), ((0, 4, 0), "^0.3", False),
    ((1, 2, 9), "~1.2", True), ((1, 3, 0), "~1.2", False), ((1, 2, 3), "~1.2.4", False),
])
def test_version_ranges(version, spec, ok):
    assert version_matches(version, spec) is ok


def test_uses_needs_a_range():
    assert parse_uses("chq-academic@^1.0") == ("chq-academic", "^1.0")
    with pytest.raises(RecipeError, match="no version range"):
        parse_uses("chq-academic")
    with pytest.raises(RecipeError, match="not a version range"):
        parse_uses("chq-academic@latest")


def test_bundled_recipe_is_valid():
    r = load_recipe_file(CHQ)
    assert r.ref == "chq-academic@1.0.0" and r.title.startswith("collectionHQ")


def test_validation_messages_name_the_path():
    data = chq()
    data["outputs"][1]["columns"][3]["field"] = "barcod"
    data["outputs"][1]["extension"] = "mrc"
    data["delivery"]["transport"] = "ftp"
    data["patron_data"] = "loans"
    data["surprise"] = 1
    problems = "\n".join(validate_recipe(data, "x.yaml"))
    assert "x.yaml: outputs.items.columns[3].field: unknown field 'barcod' (did you mean 'barcode'?)" in problems
    assert "outputs.items.extension" in problems
    assert "plain FTP" in problems
    assert "patron_data: must be none" in problems
    assert "surprise: unknown setting" in problems


def test_field_needs_must_be_declared():
    data = chq()
    data["reads"] = ["instances", "holdings", "items", "marc"]
    problems = "\n".join(validate_recipe(data, "x"))
    assert "field 'last_circ_date' needs reads: loans" in problems


def test_link_must_point_at_a_matching_column():
    data = chq()
    data["link"]["item_column"] = "Nope"
    assert any("link.item_column" in p for p in validate_recipe(data, "x"))
    data = chq()
    del data["link"]
    assert any(p.endswith("link: required when a recipe has both marc21 and delimited outputs (how items match bibs)")
               for p in validate_recipe(data, "x"))


def test_shared_recipe_cannot_set_where_files_go_or_which_key_to_trust():
    data = chq()
    data["delivery"].update({"host": "evil.example", "host_key_fingerprint": "SHA256:" + "a" * 43})
    problems = "\n".join(validate_recipe(data, "x"))
    assert "delivery.host: unknown setting" in problems
    assert "delivery.host_key_fingerprint: unknown setting" in problems


def test_changelog_must_cover_the_version(tmp_path):
    data = chq()
    data["version"] = "1.1.0"
    with pytest.raises(RecipeError, match="no entry for version 1.1.0"):
        load_recipe_file(write_recipe(tmp_path, data))


def test_file_name_must_match_version(tmp_path):
    data = chq()
    path = write_yaml(tmp_path / "chq-academic" / "1.0.1.yaml", data)
    with pytest.raises(RecipeError, match="must match the file name"):
        load_recipe_file(path)


def test_library_picks_highest_match_and_reports_newer_major(tmp_path):
    for version in ("1.1.0", "2.0.0"):
        data = chq()
        data["version"] = version
        data["changelog"] = [{"version": version, "date": "2026-10-27", "changes": "test"}]
        write_recipe(tmp_path, data)
    bad = chq()
    bad["version"] = "1.2.0"
    write_recipe(tmp_path, bad)                                     # no changelog entry: reported, not used
    lib = RecipeLibrary([tmp_path, BUNDLED_RECIPES])
    assert lib.resolve("chq-academic@^1.0").version_str == "1.1.0"
    assert lib.resolve("chq-academic@1.0.0").path == CHQ
    assert lib.newer_major("chq-academic@^1.0").version_str == "2.0.0"
    assert lib.newer_major("chq-academic@^2.0") is None
    assert any("1.2.0.yaml" in p for p in lib.problems())
    with pytest.raises(RecipeError, match="versions available: 1.0.0, 1.1.0, 2.0.0"):
        lib.resolve("chq-academic@^3.0")


def test_site_recipe_folder_wins_on_identical_version(tmp_path):
    data = chq()
    data["title"] = "Local copy"
    write_recipe(tmp_path, data)
    lib = RecipeLibrary([tmp_path, BUNDLED_RECIPES])
    assert lib.resolve("chq-academic@1.0.0").title == "Local copy"


def make(tmp_path, **overrides) -> Path:
    data = feed_data(tmp_path, **overrides)
    return write_yaml(tmp_path / "feeds" / "chq.yaml", data)


def lib():
    return RecipeLibrary([BUNDLED_RECIPES])


def test_feed_overlay_rules(tmp_path):
    path = make(tmp_path, filters={"exclude_item_statuses": ["Withdrawn"]}, options={"timezone": "America/Detroit"},
                outputs={"items": {"empty_value": "", "date_format": "%Y-%m-%d"}},
                checks={"min_item_ratio": 0.5, "must_be_filled": ["item_hrid"]})
    f = load_feed(path, lib())
    assert f.selection["exclude_item_statuses"] == ["Withdrawn"]
    assert f.selection["exclude_location_codes"] == ["ONLINE"]
    assert f.selection["include_items_without_barcode"] is True     # from the recipe
    assert f.options == {"last_circ_date_from": "checkout", "loan_window_months": 12, "timezone": "America/Detroit"}
    items = f.output("items")
    assert items["empty_value"] == "" and items["delimiter"] == "|" and len(items["columns"]) == 20
    assert f.checks["min_item_ratio"] == 0.5
    assert f.checks["must_be_filled"] == ["instance_id", "status", "material_type", "item_hrid"]  # added, not replaced
    assert f.delivery["transport"] == "local" and f.delivery["upload_mode"] == "direct"
    assert f.file_prefix == "test" and f.schedule == "Sun 02:15" and f.enabled


def test_feed_cannot_change_output_format_or_add_outputs(tmp_path):
    path = make(tmp_path, outputs={"items": {"format": "marc21"}, "extra": {"extension": "txt"}})
    with pytest.raises(ConfigError) as err:
        load_feed(path, lib())
    assert "outputs.items.format: is set by the recipe" in str(err.value)
    assert "the recipe has no output called 'extra'" in str(err.value)


def test_feed_rejects_secrets_and_unquoted_codes(tmp_path):
    path = make(tmp_path, delivery={"transport": "local", "directory": "x", "password": "hunter2"})
    with pytest.raises(ConfigError, match="secrets never go in files"):
        load_feed(path, lib())
    path.write_text(path.read_text(encoding="utf-8").replace("password: hunter2", "secret: keyring"),
                    encoding="utf-8")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["filters"]["exclude_location_codes"] = [False]                # what YAML makes of an unquoted NO
    write_yaml(path, data)
    with pytest.raises(ConfigError, match="put each code in quotes"):
        load_feed(path, lib())


def test_must_fill_barcode_conflicts_with_keeping_items_without_one(tmp_path):
    path = make(tmp_path, checks={"must_be_filled": ["barcode"]})
    with pytest.raises(ConfigError, match="every run would be blocked"):
        load_feed(path, lib())


def test_sftp_feed_needs_a_pinned_key(tmp_path):
    path = make(tmp_path, delivery={"transport": "sftp", "host": "h", "username": "u", "directory": None})
    with pytest.raises(ConfigError, match="host_key_fingerprint: required"):
        load_feed(path, lib())


def test_relative_paths_are_taken_from_the_site_folder(tmp_path, monkeypatch):
    path = make(tmp_path, delivery={"transport": "local", "directory": "outbox"})
    monkeypatch.chdir(Path(tmp_path).anchor)                         # the scheduler starts somewhere else
    f = load_feed(path, lib(), tmp_path)
    assert Path(f.delivery["directory"]) == (tmp_path / "outbox").resolve()


def test_reserved_and_bad_feed_names(tmp_path):
    for name in ("folio", "Bad_Name"):
        p = write_yaml(tmp_path / "feeds" / f"{name}.yaml", feed_data(tmp_path))
        with pytest.raises(ConfigError):
            load_feed(p, lib())


def test_bare_id_in_uses_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="no version range"):
        load_feed(make(tmp_path, uses="chq-academic"), lib())


def test_recipe_data_is_not_mutated_by_feeds(tmp_path):
    library = lib()
    before = copy.deepcopy(library.resolve("chq-academic@^1.0").data)
    load_feed(make(tmp_path, outputs={"items": {"columns": [
        {"header": h, "field": f} for h, f in (("Bib record ID", "instance_id"), ("Barcode", "barcode"),
                                               ("Status", "status"), ("Material_type", "material_type"))]}}), library)
    assert library.resolve("chq-academic@^1.0").data == before
