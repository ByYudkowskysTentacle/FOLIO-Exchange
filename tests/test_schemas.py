"""The JSON Schemas (for editors) stay in step with the Python rules, the bundled recipes and the examples."""
import json
from pathlib import Path

import jsonschema
import pytest
import yaml

from folio_exchange import config, recipe
from folio_exchange.config import BUNDLED_RECIPES, load_site, load_yaml_text
from folio_exchange.recipe import RecipeLibrary, load_feed

ROOT = Path(__file__).parent.parent
SCHEMAS = ROOT / "src" / "folio_exchange" / "schemas"


def schema(name):
    return json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))


def test_schema_keys_match_the_code():
    assert set(schema("recipe")["properties"]) == set(recipe.RECIPE_KEYS)
    assert set(schema("feed")["properties"]) == set(recipe.FEED_KEYS)
    assert set(schema("feed")["properties"]["delivery"]["properties"]) == set(recipe.DELIVERY_DEFAULTS)
    assert set(schema("recipe")["properties"]["delivery"]["properties"]) == set(recipe.RECIPE_DELIVERY_KEYS)
    site = schema("site")
    for section, defaults in config.SITE_DEFAULTS.items():
        assert set(site["properties"][section]["properties"]) == set(defaults), section


@pytest.mark.parametrize("path", sorted(BUNDLED_RECIPES.glob("*/*.yaml")), ids=lambda p: p.parent.name + "/" + p.name)
def test_bundled_recipes_match_the_schema(path):
    jsonschema.validate(yaml.safe_load(path.read_text(encoding="utf-8")), schema("recipe"))


def test_examples_match_the_schemas_and_load(tmp_path):
    site_data = load_yaml_text((ROOT / "examples" / "site.yaml").read_text(encoding="utf-8"))
    feed_data = load_yaml_text((ROOT / "examples" / "feeds" / "chq.yaml").read_text(encoding="utf-8"))
    jsonschema.validate(site_data, schema("site"))
    jsonschema.validate(feed_data, schema("feed"))
    (tmp_path / "feeds").mkdir()
    (tmp_path / "site.yaml").write_text((ROOT / "examples" / "site.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "feeds" / "chq.yaml").write_text((ROOT / "examples" / "feeds" / "chq.yaml").read_text(
        encoding="utf-8"), encoding="utf-8")
    site = load_site(tmp_path)
    feed = load_feed(tmp_path / "feeds" / "chq.yaml", RecipeLibrary(site.recipe_dirs), site.root)
    assert feed.recipe.ref == "chq-academic@1.0.0" and feed.delivery["host"] == "sftp.us.collectionhq.com"
