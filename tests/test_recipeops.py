"""Custom recipes (recipeops): built from nothing or from a copy of an installed recipe, checked as any recipe is,
changed in place, and never allowed to break a feed or to change a recipe that was not built on the site."""
import copy
import json
from pathlib import Path

import jsonschema
import pytest
import yaml
from sitekit import make_feed

from folio_exchange import client as client_mod
from folio_exchange import recipeops, views
from folio_exchange.cli import main
from folio_exchange.config import BUNDLED_RECIPES, load_site
from folio_exchange.errors import ConfigError
from folio_exchange.history import History
from folio_exchange.recipe import RecipeLibrary, load_all

SCHEMA = Path(__file__).resolve().parent.parent / "src" / "folio_exchange" / "schemas" / "recipe.schema.json"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)


def loaded(root):
    site = load_site(root)
    library = RecipeLibrary(site.recipe_dirs)
    return site, library, load_all(site, library)[0]


def acme(**changes) -> dict:
    """A small recipe as a builder would send it: one comma-separated file of three columns."""
    draft = recipeops.blank_draft()
    draft.update(recipe="acme-items", vendor="Acme Analytics", title="Acme – items")
    draft["outputs"][0]["columns"] = [{"header": "Barcode", "field": "barcode"},
                                      {"header": "Title", "field": "title"},
                                      {"header": "Status", "field": "status", "required": True}]
    draft.update(changes)
    return draft


def save(root, draft, **kw) -> dict:
    return recipeops.save_custom(*loaded(root), draft, **kw)


def audit(root) -> list[dict]:
    with History(load_site(root).history_path) as hist:
        return hist.audit_log(100)


def last_run(root, feed: str) -> dict:
    with History(load_site(root).history_path) as hist:
        return hist.last_run(feed)


# ------------------------------------------------------------ a new recipe
def test_a_blank_recipe_is_saved_checked_and_listed_as_built_on_this_site(site):
    res = save(site, acme())
    assert res["ref"] == "acme-items@1.0.0" and res["new"] and res["changed"] and res["feeds"] == []
    path = site / "recipes" / "acme-items" / "1.0.0.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    jsonschema.validate(data, json.loads(SCHEMA.read_text(encoding="utf-8")))
    assert data["reads"] == ["instances", "holdings", "items"] and data["patron_data"] == "none"
    assert data["changelog"][0]["version"] == "1.0.0" and data["changelog"][0]["changes"].startswith("Built in")
    assert data["outputs"][0]["columns"][0] == {"header": "Barcode", "field": "barcode"}     # no required: false
    assert data["outputs"][0]["columns"][2]["required"] is True
    site_, library, _feeds = loaded(site)
    assert library.latest("acme-items").title == "Acme – items" and not library.problems()
    assert json.loads((site / "recipes" / "custom.json").read_text(encoding="utf-8"))["acme-items"]["file"] == \
        "acme-items/1.0.0.yaml"
    item = next(r for r in views.recipes_overview(site)["recipes"] if r["id"] == "acme-items")
    assert item["custom"] and item["versions"] == [{"version": "1.0.0", "from": "custom"}]
    bundled = next(r for r in views.recipes_overview(site)["recipes"] if r["id"] == "chq-academic")
    assert not bundled["custom"] and bundled["versions"][0]["from"] == "bundled"
    entry = next(a for a in audit(site) if a["action"] == "custom recipe added from the app")
    assert entry["detail"]["file"] == "recipes/acme-items/1.0.0.yaml" and entry["detail"]["from"] == "app"
    assert not [a for a in audit(site) if a["action"] == "settings file added"]      # not taken for a hand edit


def test_reads_follow_the_fields_and_files_the_recipe_uses(site):
    draft = acme()
    draft["outputs"][0]["columns"].append({"header": "Loans", "field": "loans_total"})
    assert recipeops.reads_for(draft) == ["instances", "holdings", "items", "loans"]
    draft["outputs"].append({"name": "bibs", "format": "marc21"})
    assert recipeops.reads_for(draft) == ["instances", "holdings", "items", "loans", "marc"]
    review = recipeops.review(*loaded(site), draft)
    assert any(p.startswith("link: required when a recipe has both") for p in review["problems"])
    draft["outputs"][0]["columns"].insert(0, {"header": "Bib", "field": "instance_id"})
    draft["link"] = {"bib_field": "999 ff $i", "item_column": "Bib", "value": "instance_id"}
    review = recipeops.review(*loaded(site), draft)
    assert review["problems"] == [] and review["reads"][-1] == "marc"
    assert "circulation-storage.loans.collection.get" in review["permissions"]
    assert [o["format"] for o in review["outputs"]] == ["delimited", "marc21"]
    assert review["outputs"][1]["item_field"]["tag"] == "949"                        # defaults filled in


def test_a_recipe_with_problems_is_not_saved_and_says_why(site):
    draft = acme()
    draft["outputs"][0]["columns"] = []
    draft["outputs"][0]["delimiter"] = "||"
    review = recipeops.review(*loaded(site), draft)
    assert any("columns: list one or more columns" in p for p in review["problems"])
    assert any("delimiter" in p for p in review["problems"])
    with pytest.raises(ConfigError, match="Not saved"):
        save(site, draft)
    assert not (site / "recipes" / "acme-items").exists()
    for key, value in (("patron_data", "full"), ("reads", ["users"]), ("version", "9.9.9")):
        with pytest.raises(ConfigError, match="cannot be set in a custom recipe"):
            save(site, {**acme(), key: value})
    assert not (site / "recipes").exists() or not list((site / "recipes").iterdir())


def test_an_id_already_in_use_or_published_by_a_vendor_is_refused(site):
    site_, library, _feeds = loaded(site)
    assert "already a recipe" in recipeops.id_problem(site_, library, "chq-academic") or \
        "publishes" in recipeops.id_problem(site_, library, "chq-academic")
    for bad in ("", "Acme", "acme items", "-acme", "a" * 61):
        assert "lowercase letters" in recipeops.id_problem(site_, library, bad)
    assert recipeops.id_problem(site_, library, "acme-items") is None
    save(site, acme())
    with pytest.raises(ConfigError, match="already a recipe called acme-items"):
        save(site, acme())
    (site / "recipes" / "half-done").mkdir()                                         # a folder with no recipe yet
    assert "half-done" in recipeops.taken_ids(*loaded(site)[:2])


# ------------------------------------------------------------ a copy
def test_a_copy_of_an_installed_recipe_writes_the_same_files(site):
    form = recipeops.builder_form(*loaded(site), copy_of="chq-academic")
    draft = form["draft"]
    assert draft["recipe"] == "chq-academic-custom" and form["copied_from"] == "chq-academic@1.0.0"
    assert form["editing"] is None and form["version"] == "" and "chq-academic" in form["taken"]
    assert {f["name"] for f in form["fields"]} >= {"barcode", "loans_total"}
    res = save(site, draft, copied_from=form["copied_from"])
    assert res["ref"] == "chq-academic-custom@1.0.0"
    data = yaml.safe_load((site / "recipes" / "chq-academic-custom" / "1.0.0.yaml").read_text(encoding="utf-8"))
    original = yaml.safe_load((BUNDLED_RECIPES / "chq-academic" / "1.0.0.yaml").read_text(encoding="utf-8"))
    assert data["reads"] == original["reads"] and data["link"] == original["link"]
    assert data["changelog"][0]["changes"].startswith("Copied from chq-academic@1.0.0")
    make_feed(site, "mine", uses="chq-academic-custom@^1.0",
              delivery={"transport": "local", "directory": str(site / "other-drop")})
    assert main(["-s", str(site), "run", "chq"]) == 2 and main(["-s", str(site), "run", "mine"]) == 2
    theirs, mine = Path(last_run(site, "chq")["run_dir"]), Path(last_run(site, "mine")["run_dir"])
    files = sorted(p.name for p in theirs.iterdir() if p.suffix in (".mrc", ".psv"))
    assert len(files) == 2
    for name in files:
        assert (mine / name).read_bytes() == (theirs / name).read_bytes(), name


def test_only_a_recipe_built_here_can_be_changed(site):
    with pytest.raises(ConfigError, match="wasn't built on this site"):
        recipeops.builder_form(*loaded(site), edit="chq-academic")
    with pytest.raises(ConfigError, match="wasn't built on this site"):
        save(site, {**acme(), "recipe": "chq-academic"}, editing="chq-academic")
    by_hand = site / "recipes" / "by-hand" / "1.0.0.yaml"                            # placed by hand, not built here
    by_hand.parent.mkdir(parents=True)
    by_hand.write_text((BUNDLED_RECIPES / "chq-academic" / "1.0.0.yaml").read_text(encoding="utf-8")
                       .replace("recipe: chq-academic", "recipe: by-hand"), encoding="utf-8")
    with pytest.raises(ConfigError, match="wasn't built on this site"):
        recipeops.builder_form(*loaded(site), edit="by-hand")
    assert recipeops.builder_form(*loaded(site), copy_of="by-hand")["draft"]["recipe"] == "by-hand-custom"
    with pytest.raises(LookupError):
        recipeops.builder_form(*loaded(site), edit="nothing-here")


# ------------------------------------------------------------ changing one in place
def custom_feed(site) -> None:
    """A feed on a custom copy of chq-academic that has delivered once."""
    form = recipeops.builder_form(*loaded(site), copy_of="chq-academic")
    save(site, form["draft"], copied_from=form["copied_from"])
    make_feed(site, uses="chq-academic-custom@^1.0")
    assert main(["-s", str(site), "run", "chq"]) == 2
    assert main(["-s", str(site), "release", "chq", "--yes"]) == 0


def test_a_change_in_place_reaches_its_feeds_and_holds_a_changed_layout(site):
    custom_feed(site)
    form = recipeops.builder_form(*loaded(site), edit="chq-academic-custom")
    assert form["editing"] == "chq-academic-custom" and form["feeds"] == ["chq"] and form["version"]
    draft = copy.deepcopy(form["draft"])
    same = recipeops.review(*loaded(site), draft, editing="chq-academic-custom")
    assert same["problems"] == [] and not same["changed"] and same["feeds"][0]["changes"] == []
    assert save(site, draft, editing="chq-academic-custom", version=form["version"])["changed"] is False
    items = next(o for o in draft["outputs"] if o["name"] == "items")
    items["columns"][1]["header"] = "Item ID"
    review = recipeops.review(*loaded(site), draft, editing="chq-academic-custom")
    assert review["changed"] and review["feeds"] == [{
        "name": "chq", "delivers": True, "enabled": True,
        "changes": ["items file: columns changed: 'Item record ID' renamed to 'Item ID'"]}]
    res = save(site, draft, editing="chq-academic-custom", version=form["version"])
    assert res["changed"] and not res["new"] and res["feeds"] == ["chq"] and res["held"] == ["chq"]
    data = yaml.safe_load((site / "recipes" / "chq-academic-custom" / "1.0.0.yaml").read_text(encoding="utf-8"))
    assert data["version"] == "1.0.0" and len(data["changelog"]) == 1
    assert "Last changed on" in data["changelog"][0]["changes"]
    entry = next(a for a in audit(site) if a["action"] == "custom recipe changed from the app")
    assert entry["detail"]["feeds"] == ["chq"] and entry["detail"]["layout_changed_for"] == ["chq"]
    assert main(["-s", str(site), "run", "chq"]) == 2                               # held: the layout changed
    held = last_run(site, "chq")
    assert held["status"] == "held" and "'Item record ID' renamed to 'Item ID'" in held["report"]["held"]
    again = recipeops.builder_form(*loaded(site), edit="chq-academic-custom")       # a second change keeps one line
    again["draft"]["title"] = "Mine"
    save(site, again["draft"], editing="chq-academic-custom", version=again["version"])
    data = yaml.safe_load((site / "recipes" / "chq-academic-custom" / "1.0.0.yaml").read_text(encoding="utf-8"))
    assert data["title"] == "Mine" and data["changelog"][0]["changes"].count("Last changed on") == 1


def test_a_change_that_would_stop_a_feed_loading_is_refused(site):
    custom_feed(site)
    make_feed(site, uses="chq-academic-custom@^1.0", outputs={"items": {"empty_value": ""}})
    path = site / "recipes" / "chq-academic-custom" / "1.0.0.yaml"
    before = path.read_bytes()
    form = recipeops.builder_form(*loaded(site), edit="chq-academic-custom")
    draft = form["draft"]
    next(o for o in draft["outputs"] if o["name"] == "items")["name"] = "rows"
    review = recipeops.review(*loaded(site), draft, editing="chq-academic-custom")
    assert len(review["problems"]) == 1 and review["problems"][0].startswith("The feed chq would stop loading:")
    assert "no output called 'items'" in review["problems"][0]
    with pytest.raises(ConfigError, match="The feed chq would stop loading"):
        save(site, draft, editing="chq-academic-custom", version=form["version"])
    assert path.read_bytes() == before and not load_all(load_site(site))[1]
    with pytest.raises(ConfigError, match="id can't change"):
        save(site, {**form["draft"], "recipe": "another-id"}, editing="chq-academic-custom")


def test_a_file_changed_underneath_is_not_overwritten_unless_asked(site):
    save(site, acme())
    path = site / "recipes" / "acme-items" / "1.0.0.yaml"
    form = recipeops.builder_form(*loaded(site), edit="acme-items")
    path.write_text(path.read_text(encoding="utf-8").replace("Acme – items", "Acme – by hand"), encoding="utf-8")
    draft = {**form["draft"], "vendor": "Acme Ltd"}
    with pytest.raises(ConfigError, match="changed since this form was opened"):
        save(site, draft, editing="acme-items", version=form["version"])
    assert "Acme – by hand" in path.read_text(encoding="utf-8")
    assert save(site, draft, editing="acme-items", version=None)["changed"]
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["vendor"] == "Acme Ltd" and data["title"] == "Acme – items"


def test_a_site_without_a_recipes_folder_of_its_own_says_so(site):
    text = (site / "site.yaml").read_text(encoding="utf-8")
    (site / "site.yaml").write_text(text + "paths:\n  recipes: .\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="this site's own recipes folder"):
        save(site, acme())
