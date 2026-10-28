"""Vendor recipes from GitHub: a curated list of sources, downloads pinned to a commit, updates on request.

A source is a folder in a GitHub repository holding ``<recipe-id>/<version>.yaml`` files, such as cHQ FOLIO Exchange Recipe's
``recipes`` folder. The list ships with FOLIO-Exchange (``recipe_sources.yaml``; vendors are added by pull
request), and a site may add its own under ``recipe_sources`` in ``site.yaml``. Nothing else is downloaded.

``check`` looks at the branch as it is now, records its commit, and reads every recipe file there without
installing anything, so a person can review what would change. ``install`` then takes exactly that commit.
Only versions that are not installed yet are written, into the site's own ``recipes`` folder: a published
version never changes, so a file that differs from an installed copy of the same version is refused, never
swapped in. A source may only provide the recipe ids it lists. Every file must load as a recipe, which
also means it reads no patron data. Where each version came from is kept in ``recipes/sources.json``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import requests
import yaml

from . import __version__
from .config import BUNDLED_RECIPES, Site
from .errors import ConfigError, FxError, RecipeError
from .history import History
from .recipe import ID_RE, Feed, Recipe, RecipeLibrary, load_recipe_file, parse_uses, version_matches

SOURCES_FILE = Path(__file__).parent / "recipe_sources.yaml"
PROVENANCE_FILE = "sources.json"
API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
HOSTS = {"api.github.com", "raw.githubusercontent.com"}
MAX_FILE_BYTES = 256 * 1024
MAX_FILES = 100
TIMEOUT = 30

_REPO = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9._-]+$")
_BRANCH = re.compile(r"^[A-Za-z0-9._/-]+$")
_FOLDER = re.compile(r"^[A-Za-z0-9._/-]*$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
# What a person reads about a recipe, not what it does: the only parts a minor or patch version may change for
# feeds that already use it. (default_schedule is not wording: a feed file without a schedule of its own follows it.)
_WORDING = {"version", "changelog", "description", "title", "spec_source"}
_FILE = re.compile(r"^([a-z0-9][a-z0-9-]*)/(\d+\.\d+\.\d+)\.yaml$")


@dataclass
class Source:
    id: str
    vendor: str
    title: str
    repo: str
    recipes: list[str]
    branch: str = "main"
    path: str = "recipes"
    description: str = ""
    homepage: str | None = None
    origin: str = "bundled"                       # bundled (the curated list) or site (site.yaml)
    problems: list[str] = field(default_factory=list)


def source_problems(entry: Any, where: str) -> list[str]:
    """What is wrong with one source entry (an empty list when nothing is)."""
    if not isinstance(entry, dict):
        return [f"{where}: expected a mapping with id, vendor, title, repo and recipes"]
    p = []
    allowed = {"id", "vendor", "title", "description", "repo", "branch", "path", "recipes", "homepage"}
    p += [f"{where}.{k}: unknown setting" for k in entry if k not in allowed]
    if not ID_RE.match(str(entry.get("id") or "")):
        p.append(f"{where}.id: lowercase letters, digits and hyphens")
    for key in ("vendor", "title"):
        if not str(entry.get(key) or "").strip():
            p.append(f"{where}.{key}: required")
    if not _REPO.match(str(entry.get("repo") or "")):
        p.append(f"{where}.repo: a GitHub repository as owner/name")
    branch = str(entry.get("branch") or "main")
    if not _BRANCH.match(branch) or ".." in branch:
        p.append(f"{where}.branch: a branch name")
    folder = str(entry.get("path") if entry.get("path") is not None else "recipes")
    if not _FOLDER.match(folder) or ".." in folder.split("/"):
        p.append(f"{where}.path: a folder in the repository, such as recipes")
    ids = entry.get("recipes")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and ID_RE.match(i) for i in ids):
        p.append(f"{where}.recipes: the recipe ids this source may provide, e.g. [chq-academic]")
    home = entry.get("homepage")
    if home is not None and not str(home).startswith("https://"):
        p.append(f"{where}.homepage: an https:// address")
    return p


def _source(entry: dict, origin: str) -> Source:
    return Source(id=entry["id"], vendor=str(entry["vendor"]), title=str(entry["title"]), repo=entry["repo"],
                  recipes=list(entry["recipes"]), branch=str(entry.get("branch") or "main"),
                  path=str(entry.get("path") if entry.get("path") is not None else "recipes").strip("/"),
                  description=str(entry.get("description") or ""), homepage=entry.get("homepage"), origin=origin)


def load_sources(site: Site | None = None) -> list[Source]:
    """The curated sources, then the site's own. A site source cannot reuse a curated source's id."""
    data = yaml.safe_load(SOURCES_FILE.read_text(encoding="utf-8")) or {}
    out: list[Source] = []
    for n, entry in enumerate(data.get("sources") or []):
        problems = source_problems(entry, f"recipe_sources.yaml sources[{n}]")
        if problems:
            raise ConfigError("\n".join(problems))
        out.append(_source(entry, "bundled"))
    taken = {s.id for s in out}
    for n, entry in enumerate((site.recipe_sources if site else None) or []):
        if entry.get("id") in taken:
            raise ConfigError(f"site.yaml recipe_sources[{n}].id: {entry['id']!r} is already a curated source")
        out.append(_source(entry, "site"))
        taken.add(entry["id"])
    return out


def find_source(site: Site, source_id: str) -> Source:
    for s in load_sources(site):
        if s.id == source_id:
            return s
    known = ", ".join(s.id for s in load_sources(site)) or "none"
    raise ConfigError(f"No recipe source called {source_id!r} (sources: {known}).")


# ------------------------------------------------------------------ where installed versions came from
def _recipes_dir(site: Site) -> Path:
    """The site's own recipes folder (the first of ``paths.recipes``), never the program's bundled one."""
    first = site.recipe_dirs[0].resolve() if site.recipe_dirs else None
    if first is None or first == BUNDLED_RECIPES.resolve() or first == site.root.resolve():
        raise ConfigError("Downloaded recipes go in this site's own recipes folder, but paths.recipes in site.yaml "
                          "does not start with one (it is empty, or names the site folder itself). Set "
                          "paths.recipes: recipes.")
    return first


def provenance(site: Site) -> dict:
    try:
        path = _recipes_dir(site) / PROVENANCE_FILE
    except ConfigError:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_provenance(site: Site, data: dict) -> None:
    path = _recipes_dir(site) / PROVENANCE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ------------------------------------------------------------------ GitHub
def _get(http: Any, url: str, what: str) -> Any:
    """One GET to GitHub over https. Redirects are followed by hand, and only within GitHub's hosts."""
    for _hop in range(4):
        parts = urlparse(url)
        if parts.scheme != "https" or parts.hostname not in HOSTS:
            raise FxError(f"Refused to download from {parts.hostname}: only GitHub is used.")
        try:
            r = http.get(url, timeout=TIMEOUT, allow_redirects=False,
                         headers={"Accept": "application/vnd.github+json",
                                  "User-Agent": f"folio-exchange/{__version__}"})
        except requests.RequestException as exc:
            raise FxError(f"Could not reach GitHub for {what} ({type(exc).__name__}). Check this computer's "
                          "internet connection, or its proxy settings.") from None
        if r.status_code not in (301, 302, 303, 307, 308):
            break
        url = urljoin(url, (getattr(r, "headers", None) or {}).get("Location") or "")
    else:
        raise FxError(f"GitHub redirected {what} too many times.")
    if urlparse(getattr(r, "url", url) or url).hostname not in HOSTS:
        raise FxError("GitHub redirected the download to another site, so it was refused.")
    if r.status_code in (403, 429) and "rate limit" in (r.text or "").lower():
        raise FxError("GitHub's limit for downloads without an account has been reached. Try again in an hour.")
    if r.status_code == 404:
        raise FxError(f"GitHub has no {what} (404). The vendor may have moved it; check the source's settings.")
    if r.status_code != 200:
        raise FxError(f"GitHub answered {r.status_code} for {what}.")
    if len(r.content or b"") > MAX_FILE_BYTES * MAX_FILES:
        raise FxError(f"The answer for {what} is too large.")
    return r


def _session() -> requests.Session:
    s = requests.Session()
    s.max_redirects = 3
    return s


# ------------------------------------------------------------------ check and install
def _same_text(a: bytes, b: bytes) -> bool:
    """The same file apart from line endings and a byte-order mark (a Windows checkout may store CRLF)."""
    def norm(x: bytes) -> bytes:
        return x.removeprefix(b"\xef\xbb\xbf").replace(b"\r\n", b"\n")
    return norm(a) == norm(b)


def _status(library: RecipeLibrary, rid: str, version: tuple[int, int, int], content: bytes) -> str:
    """new, installed (the same file) or conflict (another file under the same version)."""
    for r in library.versions(rid):
        if r.version == version:
            try:
                same = _same_text(r.path.read_bytes(), content)
            except OSError:
                same = False
            return "installed" if same else "conflict"
    return "new"


def _feeds_picking_up(site: Site, rid: str, version: tuple[int, int, int], library: RecipeLibrary) -> list:
    """Feeds whose ``uses`` range would take this version from their next run."""
    from .recipe import load_all

    takers = []
    for f in load_all(site, library)[0]:
        fid, spec = parse_uses(f.uses)
        if fid == rid and version_matches(version, spec) and version > f.recipe.version:
            takers.append(f)
    return takers


def _changes_substance(new: dict, old: dict) -> list[str]:
    """The parts of a recipe, other than its wording, that differ between two versions."""
    keys = (set(new) | set(old)) - _WORDING
    return sorted(k for k in keys if new.get(k) != old.get(k))


def _downloaded(site: Site, recipe: Recipe) -> bool:
    """True when sources.json records downloading this recipe file and the file is still as downloaded."""
    name = f"{recipe.id}/{recipe.version_str}.yaml"
    try:
        digest = hashlib.sha256(recipe.path.read_bytes()).hexdigest()
    except OSError:
        return False
    return any(isinstance(e, dict) and ((e.get("files") or {}).get(name) or {}).get("sha256") == digest
               for e in provenance(site).values())


def local_minor_warning(site: Site, library: RecipeLibrary, feed: Feed) -> str | None:
    """A warning when ``feed`` uses a recipe version placed in the site's recipes folder by hand (not downloaded
    from a recipe source, or changed since) that changes more than wording compared with the version the feed
    would use otherwise. A downloaded minor version like that is refused; a hand-placed one is taken from the
    next run, without the review and held first delivery a new major version gets."""
    r = feed.recipe
    if r.path.resolve().is_relative_to(BUNDLED_RECIPES.resolve()) or _downloaded(site, r):
        return None
    _rid, spec = parse_uses(feed.uses)
    lower = [x for x in library.versions(r.id) if x.version < r.version and version_matches(x.version, spec)]
    if not lower:
        return None
    differs = _changes_substance(r.data, lower[-1].data)
    if not differs:
        return None
    return (f"{r.path} ({r.ref}) was not downloaded from a recipe source, and it changes {', '.join(differs)} "
            f"compared with {lower[-1].ref}, which {feed.name} would use otherwise. As a minor version it is used "
            "from the next run, without the review and held first delivery a new major version gets. If the change "
            f"is meant, give it the version {r.version[0] + 1}.0.0 and run `folio-exchange upgrade {feed.name}`; if "
            "not, delete the file.")


def check(site: Site, library: RecipeLibrary, source_id: str, *, commit: str | None = None,
          http: Any = None) -> dict:
    """What the source offers at its branch's current commit (or ``commit``), compared with what is installed.
    Downloads and checks every recipe file, but installs nothing."""
    return _check(site, library, source_id, commit, http)[0]


def _check(site: Site, library: RecipeLibrary, source_id: str, commit: str | None, http: Any
           ) -> tuple[dict, dict[str, bytes]]:
    src = find_source(site, source_id)
    http = http or _session()
    if commit is None:
        head = _get(http, f"{API}/repos/{src.repo}/commits/{quote(src.branch, safe='')}",
                    f"the {src.branch} branch of {src.repo}").json()
        commit = str(head.get("sha") or "")
    if not _SHA.match(commit or ""):
        raise FxError(f"GitHub did not give a commit for {src.repo} ({commit!r}).")
    tree = _get(http, f"{API}/repos/{src.repo}/git/trees/{commit}?recursive=1", f"the files of {src.repo}").json()
    if tree.get("truncated"):
        raise FxError(f"{src.repo} has too many files to list; ask the vendor to keep recipes in a smaller "
                      "repository.")
    prefix = f"{src.path}/" if src.path else ""
    found, problems = [], []
    for entry in tree.get("tree") or []:
        path = str(entry.get("path") or "")
        if entry.get("type") != "blob" or not path.startswith(prefix):
            continue
        m = _FILE.fullmatch(path[len(prefix):])
        if not m:
            continue
        rid, ver = m.groups()
        if any((rid, ver) == (f[0], f[1]) for f in found):
            problems.append(f"{path}: a second file for {rid} {ver}; skipped")
        elif rid not in src.recipes:
            problems.append(f"{path}: {src.id} may not provide recipe {rid!r}; skipped")
        elif int(entry.get("size") or 0) > MAX_FILE_BYTES:
            problems.append(f"{path}: larger than {MAX_FILE_BYTES // 1024} KB; skipped")
        else:
            found.append((rid, ver, path))
    if len(found) > MAX_FILES:
        raise FxError(f"{src.repo} offers more than {MAX_FILES} recipe files; refused.")
    recipes, contents = [], {}
    with tempfile.TemporaryDirectory(prefix="fx-recipes-") as tmp:
        for rid, ver, path in sorted(found):
            content = contents[path] = _get(http, f"{RAW}/{src.repo}/{commit}/{quote(path)}", path).content
            item: dict[str, Any] = {"id": rid, "version": ver, "path": path,
                                    "sha256": hashlib.sha256(content).hexdigest()}
            if len(content) > MAX_FILE_BYTES:
                item.update(status="invalid", problem="file too large")
                recipes.append(item)
                continue
            target = Path(tmp) / rid / f"{ver}.yaml"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            try:
                r = load_recipe_file(target)
                if r.id != rid or r.version_str != ver:
                    raise RecipeError(f"the file says {r.id} {r.version_str}, but is named {rid}/{ver}.yaml")
            except (RecipeError, ConfigError) as exc:
                item.update(status="invalid", problem=str(exc))
                recipes.append(item)
                continue
            status = _status(library, rid, r.version, content)
            change = next((c for c in r.data.get("changelog") or [] if str(c.get("version")) == ver), {})
            takers = _feeds_picking_up(site, rid, r.version, library) if status == "new" else []
            item.update(status=status, title=r.title, vendor=r.data.get("vendor"), reads=r.data.get("reads"),
                        patron_data=r.data.get("patron_data"), changes=change.get("changes"),
                        picked_up_by=[f.name for f in takers])
            differs = sorted({k for f in takers for k in _changes_substance(r.data, f.recipe.data)})
            if differs:
                item.update(status="invalid", problem=(
                    f"{rid} {ver} changes {', '.join(differs)} compared with the version that feeds "
                    f"{', '.join(item['picked_up_by'])} use now. As a minor version those feeds would take it from "
                    "their next run, without the review and held first delivery a new major version gets, so it "
                    f"is refused. Ask the vendor to publish it as {r.version[0] + 1}.0.0."))
            if status == "conflict":
                item["problem"] = (f"{rid} {ver} is already installed with different contents. A published version "
                                   "must not change, so this file is refused; ask the vendor to publish a new "
                                   "version instead.")
            recipes.append(item)
    installed = provenance(site).get(src.id) or {}
    return {"source": {k: v for k, v in asdict(src).items() if k != "problems"}, "commit": commit,
            "installed_commit": installed.get("commit"), "checked": _now(), "recipes": recipes,
            "problems": problems, "new": [f"{r['id']}@{r['version']}" for r in recipes if r["status"] == "new"]
            }, contents


def install(site: Site, library: RecipeLibrary, source_id: str, commit: str, *, http: Any = None,
            via: str = "cli") -> dict:
    """Install the new versions the source offers at ``commit`` (the one a person reviewed with ``check``)."""
    if not _SHA.match(commit or ""):
        raise ConfigError("Say which commit to install (from the check).")
    folder = _recipes_dir(site)
    result, contents = _check(site, library, source_id, commit, http)
    src = result["source"]
    written = []
    for item in result["recipes"]:
        if item["status"] != "new":
            continue
        target = folder / item["id"] / f"{item['version']}.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(contents[item["path"]])
        os.replace(tmp, target)
        written.append(item)
    data = provenance(site)
    entry = data.get(src["id"]) or {}
    files = dict(entry.get("files") or {})
    for item in written:
        files[f"{item['id']}/{item['version']}.yaml"] = {"sha256": item["sha256"], "commit": commit}
    data[src["id"]] = {"repo": src["repo"], "branch": src["branch"], "path": src["path"], "commit": commit,
                       "installed": _now(), "files": files}
    _write_provenance(site, data)
    with History(site.history_path) as hist:
        hist.audit(None, None, "recipes downloaded", {
            "source": src["id"], "repo": src["repo"], "commit": commit,
            "versions": [f"{i['id']}@{i['version']}" for i in written], "from": via})
    return {**result, "installed": [f"{i['id']}@{i['version']}" for i in written]}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
