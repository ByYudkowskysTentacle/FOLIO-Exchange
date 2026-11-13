"""Custom recipes: a recipe a library builds for itself, from nothing or from a copy of an installed one.

The desktop app's recipe builder calls these, and the browser page and the command line can call them later with
the same answers. A builder works on a *draft*: a recipe's settings without the parts the core fills in. The core
works out ``reads`` from the fields the draft uses, sets ``patron_data: none``, and keeps the version and the
changelog. Whatever a draft holds is checked with the same rules as any recipe (``recipe.validate_recipe``), so a
custom recipe can do nothing a shared one cannot.

A custom recipe is one file, ``<site's recipes folder>/<id>/1.0.0.yaml``, recorded in ``custom.json`` beside it.
Unlike a published version, it is changed in place: the feeds that use it follow from their next run, and a change
to what the vendor receives holds that delivery for review, as any layout change does (``runner.hold_reason``).
A save is refused when a feed that loads today would stop loading. Recipes that were downloaded, bundled or placed
by hand are never changed here; they can be copied to a custom recipe under a new id.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import setupops, sources
from .config import BUNDLED_RECIPES, Site, merge, read_yaml, write_yaml
from .errors import ConfigError, RecipeError
from .fields import FIELDS, ITEM
from .reads import READS, permissions_for
from .recipe import (
    CHECKS_DEFAULTS,
    ID_RE,
    LINKS,
    OPTIONS_DEFAULTS,
    SELECTION_DEFAULTS,
    Feed,
    Recipe,
    RecipeLibrary,
    _with_defaults,
    collision_with,
    layout_changes,
    layout_signature,
    load_feed_data,
    load_recipe_file,
    parse_version,
    validate_recipe,
)

CUSTOM_FILE = "custom.json"
FIRST_VERSION = "1.0.0"
MAX_ID = 60
HEADER = """Custom recipe, built in FOLIO-Exchange on this site.
Change it in the app (Recipes > Edit recipe…) rather than by hand: comments here are lost when the app saves it."""
# What a builder may set. The core fills in recipe version, changelog, reads and patron_data itself.
DRAFT_KEYS = ("recipe", "vendor", "title", "description", "spec_source", "link", "file_prefix", "selection",
              "options", "outputs", "checks", "delivery", "default_schedule")
BUILT = "Built in FOLIO-Exchange."
_CHANGED = re.compile(r"\s*Last changed on \d{4}-\d{2}-\d{2}\.$")


# ------------------------------------------------------------------ where custom recipes are kept
def recipes_dir(site: Site) -> Path:
    """The site's own recipes folder (the first of ``paths.recipes``), never the program's bundled one."""
    first = site.recipe_dirs[0] if site.recipe_dirs else None
    if first is None or first.resolve() in (BUNDLED_RECIPES.resolve(), site.root.resolve()):
        raise ConfigError("Custom recipes go in this site's own recipes folder, but paths.recipes in site.yaml "
                          "does not start with one (it is empty, or names the site folder itself). Set "
                          "paths.recipes: recipes.")
    return first


def _site_rel(site: Site, path: Path) -> str:
    """A file's path as the settings audit names it: relative to the site folder when it is inside it."""
    return path.relative_to(site.root).as_posix() if path.is_relative_to(site.root) else str(path)


def custom_records(site: Site) -> dict:
    """``custom.json``: the recipes built on this site, by id. Empty when there are none or it cannot be read."""
    try:
        data = json.loads((recipes_dir(site) / CUSTOM_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, ConfigError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def _write_records(site: Site, data: dict) -> None:
    path = recipes_dir(site) / CUSTOM_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def is_custom(site: Site, recipe: Recipe, records: dict | None = None) -> bool:
    """True when ``recipe`` was built on this site (recorded in custom.json, and its file is in the site's own
    recipes folder), so the builder may change it."""
    records = custom_records(site) if records is None else records
    if recipe.id not in records:
        return False
    try:
        return recipe.path.resolve().is_relative_to(recipes_dir(site).resolve())
    except ConfigError:
        return False


def _custom_recipe(site: Site, library: RecipeLibrary, recipe_id: str) -> Recipe:
    recipe = library.latest(str(recipe_id or ""))
    if recipe is None:
        raise LookupError(f"No recipe called {recipe_id!r}")
    if not is_custom(site, recipe):
        raise ConfigError(f"{recipe.id} wasn't built on this site, so it can't be changed here. Copy it to a "
                          "custom recipe instead.")
    return recipe


# ------------------------------------------------------------------ drafts
def blank_draft() -> dict:
    """A new recipe's starting point: one comma-separated item file with no columns yet."""
    return {
        "recipe": "", "vendor": "", "title": "", "description": "", "spec_source": "", "link": None,
        "file_prefix": "",
        "selection": copy.deepcopy(SELECTION_DEFAULTS), "options": copy.deepcopy(OPTIONS_DEFAULTS),
        "outputs": [_with_defaults({"name": "items", "format": "delimited"})],
        "checks": copy.deepcopy(CHECKS_DEFAULTS),
        "delivery": {"transport": "sftp", "upload_mode": "staged"},
        "default_schedule": "Sun 03:30",
    }


def draft_of(recipe: Recipe) -> dict:
    """An installed recipe as a draft, with every default written out so a form can show it."""
    d = recipe.data
    delivery = d.get("delivery") or {}
    return {
        "recipe": recipe.id, "vendor": str(d.get("vendor") or ""), "title": recipe.title,
        "description": str(d.get("description") or "").strip(), "spec_source": str(d.get("spec_source") or ""),
        "link": copy.deepcopy(d.get("link")), "file_prefix": str(d.get("file_prefix") or ""),
        "selection": merge(SELECTION_DEFAULTS, d.get("selection")),
        "options": merge(OPTIONS_DEFAULTS, d.get("options")),
        "outputs": [_with_defaults(copy.deepcopy(o)) for o in d.get("outputs") or []],
        "checks": merge(CHECKS_DEFAULTS, d.get("checks")),
        "delivery": {"transport": delivery.get("transport", "none"),
                     "upload_mode": delivery.get("upload_mode", "staged")},
        "default_schedule": str(d.get("default_schedule") or "manual"),
    }


def _fields_used(data: dict) -> list[str]:
    used: list[Any] = []
    for o in data.get("outputs") or []:
        if not isinstance(o, dict):
            continue
        used += [c.get("field") for c in o.get("columns") or [] if isinstance(c, dict)]
        subs = (o.get("item_field") or {}).get("subfields") if isinstance(o.get("item_field"), dict) else None
        used += list(subs.values()) if isinstance(subs, dict) else []
    link = data.get("link")
    if isinstance(link, dict):
        used.append(link.get("value"))
    must = (data.get("checks") or {}).get("must_be_filled") if isinstance(data.get("checks"), dict) else None
    used += list(must) if isinstance(must, list) else []
    return [f for f in used if isinstance(f, str)]


def reads_for(data: dict) -> list[str]:
    """What a recipe must read from FOLIO for the fields and files it has: instances, holdings and items always
    (every item row joins the three), loans when a field needs them, and MARC records for a MARC file."""
    wanted = set(ITEM)
    for name in _fields_used(data):
        if name in FIELDS:
            wanted |= FIELDS[name].needs
    if any(isinstance(o, dict) and o.get("format") == "marc21" for o in data.get("outputs") or []):
        wanted.add("marc")
    return [r for r in READS if r in wanted]


def _tidy_output(out: Any) -> Any:
    """An output as it is written to the file: no ``required: false`` on a column, no settings of the other
    format. Anything that is not a mapping is left for the recipe check to refuse."""
    if not isinstance(out, dict):
        return out
    o = copy.deepcopy(out)
    if isinstance(o.get("name"), str):
        o["name"] = o["name"].strip()
    if isinstance(o.get("columns"), list):
        o["columns"] = [{k: v for k, v in c.items() if not (k == "required" and v is False)}
                        if isinstance(c, dict) else c for c in o["columns"]]
    return o


def recipe_data(draft: dict, *, version: str = FIRST_VERSION, changelog: list | None = None) -> dict:
    """The recipe file's settings for ``draft``, in the order the format lists them."""
    if not isinstance(draft, dict):
        raise ConfigError("The recipe wasn't understood: expected a set of settings.")
    unknown = sorted(set(draft) - set(DRAFT_KEYS))
    if unknown:
        raise ConfigError(f"These settings cannot be set in a custom recipe: {', '.join(map(str, unknown))}.")

    def words(key: str) -> Any:
        value = draft.get(key)
        return value.strip() if isinstance(value, str) else value

    data: dict[str, Any] = {"recipe": words("recipe") or "", "version": version, "vendor": words("vendor") or "",
                            "title": words("title") or ""}
    for key in ("description", "spec_source"):
        if words(key):
            data[key] = words(key)
    data["changelog"] = changelog or [{"version": version, "date": _today(), "changes": BUILT}]
    data["reads"] = []
    data["patron_data"] = "none"
    if draft.get("link"):
        data["link"] = copy.deepcopy(draft["link"])
    if words("file_prefix"):
        data["file_prefix"] = words("file_prefix")
    for key in ("selection", "options"):
        if draft.get(key) is not None:
            data[key] = copy.deepcopy(draft[key])
    outputs = draft.get("outputs")
    data["outputs"] = [_tidy_output(o) for o in outputs] if isinstance(outputs, list) else outputs
    for key in ("checks", "delivery"):
        if draft.get(key) is not None:
            data[key] = copy.deepcopy(draft[key])
    if words("default_schedule"):
        data["default_schedule"] = words("default_schedule")
    data["reads"] = reads_for(data)
    return data


def _today() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ checks
def taken_ids(site: Site, library: RecipeLibrary) -> list[str]:
    """Ids a new custom recipe cannot use: every installed recipe's, every folder in the site's recipes folder
    (one whose files do not load included), and every id a vendor's recipe source may provide."""
    taken = set(library.ids())
    try:
        folder = recipes_dir(site)
        taken |= {p.name for p in folder.iterdir() if p.is_dir()} if folder.is_dir() else set()
    except (ConfigError, OSError):
        pass
    for s in sources.load_sources(site):
        taken |= set(s.recipes)
    return sorted(taken)


def id_problem(site: Site, library: RecipeLibrary, recipe_id: object, *, editing: str | None = None) -> str | None:
    rid = str(recipe_id or "")
    if not ID_RE.match(rid) or len(rid) > MAX_ID:
        return f"Use lowercase letters, digits and hyphens for the recipe id, such as acme-items ({MAX_ID} at most)."
    if editing is not None:
        return None if rid == editing else "A recipe's id can't change once it's saved. Copy the recipe instead."
    for s in sources.load_sources(site):
        if rid in s.recipes:
            return f"{rid} is an id that {s.title or s.id} publishes. Choose another id."
    if rid in taken_ids(site, library):
        return f"There's already a recipe called {rid}. Choose another id."
    return None


class _Trial:
    """A recipe library in which one recipe version is replaced by a draft of it, to see how feeds would load."""

    def __init__(self, library: RecipeLibrary, recipe: Recipe) -> None:
        self.library, self.recipe = library, recipe

    def resolve(self, uses: str) -> Recipe:
        found = self.library.resolve(uses)
        same = found.id == self.recipe.id and found.version == self.recipe.version
        return self.recipe if same else found


def _effects(site: Site, library: RecipeLibrary, feeds: list[Feed], recipe: Recipe) -> tuple[list[dict], list[str]]:
    """How each feed on ``recipe``'s id and version would fare with the draft in its place: what changes in the
    files it sends, or why it would stop loading."""
    trial = _Trial(library, recipe)
    effects, problems, trials = [], [], {}
    for f in feeds:
        if f.recipe.id != recipe.id or f.recipe.version != recipe.version:
            continue
        try:
            new = trials[f.name] = load_feed_data(read_yaml(f.path), f.path, trial, site.root)
        except ConfigError as exc:
            problems.append(f"The feed {f.name} would stop loading: {_plain(str(exc), f)}")
            continue
        effects.append({"name": f.name, "delivers": f.delivers, "enabled": f.enabled,
                        "changes": layout_changes(layout_signature(f), layout_signature(new))})
    after = [trials.get(f.name, f) for f in feeds]
    for name, new in trials.items():
        clash = collision_with(new, after)
        was = next(f for f in feeds if f.name == name)
        if clash and clash != collision_with(was, feeds):
            problems.append(clash)
    return effects, list(dict.fromkeys(problems))


def _plain(message: str, feed: Feed) -> str:
    """A feed's loading problems on one line, without the file name in front of each."""
    where = f"{feed.path.parent.name}/{feed.path.name}: "
    return " ".join(line.removeprefix(where) for line in message.splitlines())


def _problems(site: Site, library: RecipeLibrary, data: dict, editing: str | None) -> list[str]:
    where = "recipe"
    out = []
    bad_id = id_problem(site, library, data.get("recipe"), editing=editing)
    if bad_id:
        out.append(bad_id)
    for p in validate_recipe(data, where):
        p = p.removeprefix(f"{where}: ")
        if not (bad_id and p.startswith("recipe:")):           # said already, in plainer words
            out.append(p)
    return out


def _existing(site: Site, library: RecipeLibrary, editing: str | None) -> Recipe | None:
    return _custom_recipe(site, library, editing) if editing else None


def _changelog(recipe: Recipe | None, version: str, copied_from: str | None) -> list:
    """One entry for the version: when it was built, and for a recipe changed since, when it was last changed."""
    if recipe is None:
        changes = f"Copied from {copied_from} and changed in FOLIO-Exchange." if copied_from else BUILT
        return [{"version": version, "date": _today(), "changes": changes}]
    log = [dict(e) for e in recipe.data.get("changelog") or [] if isinstance(e, dict)]
    mine = next((e for e in log if str(e.get("version")) == version), None)
    if mine is None:
        mine = {"version": version, "date": _today(), "changes": BUILT}
        log.insert(0, mine)
    mine["changes"] = f"{_CHANGED.sub('', str(mine.get('changes') or BUILT))} Last changed on {_today()}."
    mine["date"] = str(mine.get("date") or _today())
    return log


def _without_log(data: dict) -> dict:
    return {k: v for k, v in data.items() if k != "changelog"}


def review(site: Site, library: RecipeLibrary, feeds: list[Feed], draft: dict, *, editing: str | None = None,
           copied_from: str | None = None) -> dict:
    """What saving ``draft`` would do, without saving: its problems in plain words, what it reads from FOLIO, the
    files it writes (with every default filled in), and for a recipe being changed, what changes for each feed
    that uses it. ``changed`` is False when an existing recipe would be saved as it is."""
    existing = _existing(site, library, editing)
    version = existing.version_str if existing else FIRST_VERSION
    data = recipe_data(draft, version=version, changelog=_changelog(existing, version, copied_from))
    problems = _problems(site, library, data, editing)
    outputs = [_with_defaults(copy.deepcopy(o)) for o in data.get("outputs") or [] if isinstance(o, dict)]
    out: dict[str, Any] = {
        "recipe": data["recipe"], "version": version, "vendor": data["vendor"], "title": data["title"],
        "problems": problems, "reads": data["reads"], "permissions": permissions_for(data["reads"]),
        "file_prefix": data.get("file_prefix") or data["recipe"], "outputs": outputs, "link": data.get("link"),
        "feeds": [], "new": existing is None,
        "changed": existing is None or _without_log(data) != _without_log(
            recipe_data(draft_of(existing), version=version, changelog=[])),
    }
    if existing is not None and not problems:
        trial = Recipe(id=existing.id, version=existing.version, data=data, path=existing.path)
        out["feeds"], broken = _effects(site, library, feeds, trial)
        out["problems"] = broken
    return out


# ------------------------------------------------------------------ the builder's form, and saving
def builder_form(site: Site, library: RecipeLibrary, feeds: list[Feed], *, copy_of: str | None = None,
                 edit: str | None = None) -> dict:
    """What a recipe builder starts from: a blank draft, a copy of an installed recipe under a new id, or a
    custom recipe to change (with its file's version, for saving), and the lists a form offers."""
    version, copied, using = "", None, []
    if edit:
        recipe = _custom_recipe(site, library, edit)
        draft = draft_of(recipe)
        version = setupops.file_version(recipe.path)
        using = [f.name for f in feeds if f.recipe.id == recipe.id]
        edit = recipe.id
    elif copy_of:
        recipe = library.latest(copy_of)
        if recipe is None:
            raise LookupError(f"No recipe called {copy_of!r}")
        draft = draft_of(recipe)
        copied = recipe.ref
        taken = taken_ids(site, library)
        base = f"{recipe.id}-custom"[:MAX_ID]
        draft["recipe"] = next(c for c in (base, *(f"{base[:MAX_ID - 4]}-{n}" for n in range(2, 1000)))
                               if c not in taken)
        draft["title"] = f"{recipe.title} (custom)"
    else:
        draft = blank_draft()
    return {
        "draft": draft, "editing": edit or None, "version": version, "copied_from": copied, "feeds": using,
        "taken": taken_ids(site, library),
        "fields": [{"name": f.name, "description": f.description, "date": f.date, "loans": "loans" in f.needs}
                   for f in FIELDS.values()],
        "links": [{"bib_field": k, "value": v} for k, v in LINKS.items()],
        "statuses": list(setupops.STATUSES),
    }


def _restore(path: Path, old: bytes | None) -> None:
    if old is None:
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()                      # the folder made for it, when nothing else is in it
        except OSError:
            pass
    else:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(old)
        os.replace(tmp, path)


def save_custom(site: Site, library: RecipeLibrary, feeds: list[Feed], draft: dict, *, editing: str | None = None,
                version: str | None = None, copied_from: str | None = None, via: str = "app") -> dict:
    """Save ``draft`` as a custom recipe: a new one (``editing`` None), or a change in place to one built here.
    ``version`` is the file's fingerprint when its form was opened (``builder_form``); a file changed since is
    not overwritten unless ``version`` is None. Nothing is saved when the recipe has a problem, or when a feed
    that loads now would stop loading."""
    with setupops._SAVE:
        folder = recipes_dir(site)
        existing = _existing(site, library, editing)
        res = review(site, library, feeds, draft, editing=editing, copied_from=copied_from)
        if res["problems"]:
            raise ConfigError("Not saved. " + " ".join(p if p.endswith(".") else p + "." for p in res["problems"]))
        rid, ver = res["recipe"], res["version"]
        path = existing.path if existing else folder / rid / f"{ver}.yaml"
        rel = _site_rel(site, path)
        old = path.read_bytes() if path.exists() else None
        if existing is None and old is not None:
            raise ConfigError(f"There's already a recipe called {rid}. Choose another id.")
        if existing is not None and version is not None and hashlib.sha256(old or b"").hexdigest() != version:
            raise ConfigError(f"{rel} changed since this form was opened (by hand, or in another window). Nothing "
                              "was saved.")
        using = [f["name"] for f in res["feeds"]]
        held = [f["name"] for f in res["feeds"] if f["changes"] and f["delivers"]]
        if not res["changed"]:
            return {"recipe": rid, "version": ver, "ref": f"{rid}@{ver}", "changed": False, "new": False,
                    "feeds": using, "held": [], "file": str(path)}
        data = recipe_data(draft, version=ver, changelog=_changelog(existing, ver, copied_from))
        write_yaml(path, data, HEADER)
        try:
            saved = load_recipe_file(path)
            if saved.id != rid or saved.version != parse_version(ver):
                raise RecipeError(f"{rel} did not load as {rid} {ver}")
        except (ConfigError, OSError) as exc:
            _restore(path, old)
            raise ConfigError(f"Not saved, because the recipe would not load:\n{exc}") from None
        records = custom_records(site)
        entry = dict(records.get(rid) or {"created": _now()})
        entry.update(saved=_now(), file=f"{rid}/{ver}.yaml")
        if copied_from and existing is None:
            entry["copied_from"] = copied_from
        records[rid] = entry
        _write_records(site, records)
    detail: dict[str, Any] = {"file": rel, "recipe": f"{rid}@{ver}", "from": via}
    if copied_from and existing is None:
        detail["copied_from"] = copied_from
    if using:
        detail["feeds"] = using
    if held:
        detail["layout_changed_for"] = held
    setupops._audit(site, None, f"custom recipe {'changed' if existing else 'added'} {setupops._from(via)}", detail)
    return {"recipe": rid, "version": ver, "ref": f"{rid}@{ver}", "changed": True, "new": existing is None,
            "feeds": using, "held": held, "file": str(path)}
