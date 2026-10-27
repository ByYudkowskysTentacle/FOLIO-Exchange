"""Vendor recipes from GitHub (sources.py): checking, pinned installs, refusals. GitHub is faked; nothing goes online."""
import hashlib
import json
import types

import pytest
import requests
from sitekit import make_site

from folio_exchange import sources
from folio_exchange.cli import main
from folio_exchange.config import BUNDLED_RECIPES, load_site, write_yaml
from folio_exchange.errors import ConfigError, FxError
from folio_exchange.history import History
from folio_exchange.recipe import RecipeLibrary

REPO = "ByYudkowskysTentacle/FOLIOHQ"
SHA = "a" * 40
BUNDLED = (BUNDLED_RECIPES / "chq-academic" / "1.0.0.yaml").read_bytes().replace(b"\r\n", b"\n")   # as GitHub serves it


def newer(version="1.1.0", change="Adds a column."):
    text = BUNDLED.decode("utf-8").replace("version: 1.0.0\n", f"version: {version}\n", 1)
    text = text.replace("changelog:\n", f'changelog:\n  - version: {version}\n    date: "2026-11-01"\n'
                                        f"    changes: {change}\n", 1)
    return text.encode("utf-8")


class FakeGitHub:
    """Answers the three kinds of GitHub request sources.py makes, from a dict of path -> bytes."""

    def __init__(self, files: dict[str, bytes], sha: str = SHA, truncated: bool = False):
        self.files, self.sha, self.truncated, self.urls = files, sha, truncated, []

    def get(self, url, timeout=None, headers=None, allow_redirects=True):
        self.urls.append(url)
        if url == f"{sources.API}/repos/{REPO}/commits/main":
            return self._json({"sha": self.sha})
        if url == f"{sources.API}/repos/{REPO}/git/trees/{self.sha}?recursive=1":
            tree = [{"path": p, "type": "blob", "size": len(b)} for p, b in self.files.items()]
            return self._json({"tree": tree, "truncated": self.truncated})
        prefix = f"{sources.RAW}/{REPO}/{self.sha}/"
        if url.startswith(prefix) and url[len(prefix):] in self.files:
            return self._resp(200, self.files[url[len(prefix):]], url)
        return self._resp(404, b"Not Found", url)

    def _json(self, data):
        return self._resp(200, json.dumps(data).encode(), "https://api.github.com/x")

    @staticmethod
    def _resp(status, content, url, headers=None):
        return types.SimpleNamespace(status_code=status, content=content, text=content.decode("utf-8", "replace"),
                                     headers=headers or {}, url=url, json=lambda: json.loads(content))


@pytest.fixture
def site(tmp_path):
    root = make_site(tmp_path / "site", "https://folio.example.org")
    return load_site(root)


def lib(site):
    return RecipeLibrary(site.recipe_dirs)


def test_the_curated_list_names_foliohq():
    (src,) = sources.load_sources()
    assert (src.id, src.repo, src.branch, src.path, src.recipes) == ("foliohq", REPO, "main", "recipes",
                                                                     ["chq-academic"])


def test_check_compares_with_what_is_installed_and_installs_nothing(site):
    gh = FakeGitHub({"recipes/chq-academic/1.0.0.yaml": BUNDLED, "recipes/chq-academic/1.1.0.yaml": newer(),
                     "recipes/chq-academic/README.md": b"# readme", "tests/test_recipe.py": b""})
    result = sources.check(site, lib(site), "foliohq", http=gh)
    assert result["commit"] == SHA and result["new"] == ["chq-academic@1.1.0"]
    by_version = {r["version"]: r for r in result["recipes"]}
    assert by_version["1.0.0"]["status"] == "installed"
    assert by_version["1.1.0"]["changes"] == "Adds a column." and by_version["1.1.0"]["patron_data"] == "none"
    assert not (site.recipe_dirs[0] / "chq-academic").exists()


def test_install_takes_exactly_the_reviewed_commit_and_records_it(site):
    gh = FakeGitHub({"recipes/chq-academic/1.1.0.yaml": newer()})
    done = sources.install(site, lib(site), "foliohq", SHA, http=gh, via="ui")
    assert done["installed"] == ["chq-academic@1.1.0"]
    assert not any("/commits/" in u for u in gh.urls)              # no second look at the branch
    target = site.recipe_dirs[0] / "chq-academic" / "1.1.0.yaml"
    assert target.read_bytes() == newer()
    got = sources.provenance(site)["foliohq"]
    assert got["commit"] == SHA and got["files"]["chq-academic/1.1.0.yaml"]["sha256"] == \
        hashlib.sha256(newer()).hexdigest()
    assert lib(site).resolve("chq-academic@^1.0").version_str == "1.1.0"
    with History(site.history_path) as hist:
        entry = hist.audit_log(10)[0]
    assert entry["action"] == "recipes downloaded" and entry["detail"]["versions"] == ["chq-academic@1.1.0"]
    again = sources.check(site, lib(site), "foliohq", http=FakeGitHub({"recipes/chq-academic/1.1.0.yaml": newer()}))
    assert again["new"] == [] and again["installed_commit"] == SHA


def test_a_changed_copy_of_an_installed_version_is_refused(site):
    changed = BUNDLED.replace(b"title: collectionHQ Academic", b"title: Something else", 1)
    result = sources.check(site, lib(site), "foliohq",
                           http=FakeGitHub({"recipes/chq-academic/1.0.0.yaml": changed}))
    (item,) = result["recipes"]
    assert item["status"] == "conflict" and "must not change" in item["problem"] and result["new"] == []
    sources.install(site, lib(site), "foliohq", SHA, http=FakeGitHub({"recipes/chq-academic/1.0.0.yaml": changed}))
    assert not (site.recipe_dirs[0] / "chq-academic" / "1.0.0.yaml").exists()


def test_files_that_are_not_valid_recipes_are_refused(site):
    patron = newer("1.2.0").replace(b"patron_data: none", b"patron_data: loans", 1)
    misnamed = newer("1.3.0")
    gh = FakeGitHub({"recipes/chq-academic/1.2.0.yaml": patron, "recipes/chq-academic/1.4.0.yaml": misnamed,
                     "recipes/chq-academic/1.5.0.yaml": b"recipe: [unclosed"})
    result = sources.check(site, lib(site), "foliohq", http=gh)
    assert {r["version"]: r["status"] for r in result["recipes"]} == {"1.2.0": "invalid", "1.4.0": "invalid",
                                                                       "1.5.0": "invalid"}
    assert "patron" in result["recipes"][0]["problem"]
    assert "must match the file name" in result["recipes"][1]["problem"]
    assert result["new"] == []


def test_a_source_only_provides_its_own_recipes(site):
    gh = FakeGitHub({"recipes/oclc-worldshare/1.0.0.yaml": newer()})
    result = sources.check(site, lib(site), "foliohq", http=gh)
    assert result["recipes"] == [] and "may not provide recipe 'oclc-worldshare'" in result["problems"][0]


def test_github_trouble_is_explained(site):
    class Down:
        def get(self, url, timeout=None, headers=None, allow_redirects=True):
            raise requests.ConnectionError("offline")

    with pytest.raises(FxError, match="internet connection"):
        sources.check(site, lib(site), "foliohq", http=Down())

    class Limited(FakeGitHub):
        def get(self, url, timeout=None, headers=None, allow_redirects=True):
            return self._resp(403, b'{"message": "API rate limit exceeded"}', url)

    with pytest.raises(FxError, match="Try again in an hour"):
        sources.check(site, lib(site), "foliohq", http=Limited({}))
    with pytest.raises(FxError, match="too many files"):
        sources.check(site, lib(site), "foliohq", http=FakeGitHub({}, truncated=True))

    class Elsewhere(FakeGitHub):
        def get(self, url, timeout=None, headers=None, allow_redirects=True):
            r = super().get(url, timeout, headers, allow_redirects)
            r.url = "https://evil.example/x"
            return r

    with pytest.raises(FxError, match="another site"):
        sources.check(site, lib(site), "foliohq", http=Elsewhere({}))
    with pytest.raises(ConfigError, match="Say which commit"):
        sources.install(site, lib(site), "foliohq", "main", http=FakeGitHub({}))
    with pytest.raises(ConfigError, match="No recipe source called 'nope'"):
        sources.check(site, lib(site), "nope", http=FakeGitHub({}))


def test_a_site_can_add_its_own_sources_but_not_shadow_the_curated_ones(tmp_path):
    entry = {"id": "ourvendor", "vendor": "Our vendor", "title": "Our vendor", "repo": "us/recipes",
             "recipes": ["our-feed"]}
    root = make_site(tmp_path / "a", "https://folio.example.org", recipe_sources=[entry])
    assert [s.id for s in sources.load_sources(load_site(root))] == ["foliohq", "ourvendor"]
    bad = make_site(tmp_path / "b", "https://folio.example.org",
                    recipe_sources=[{**entry, "repo": "https://github.com/us/recipes", "path": "../x"}])
    with pytest.raises(ConfigError, match=r"recipe_sources\[0\]\.repo.*\n.*recipe_sources\[0\]\.path"):
        load_site(bad)
    clash = make_site(tmp_path / "c", "https://folio.example.org", recipe_sources=[{**entry, "id": "foliohq"}])
    with pytest.raises(ConfigError, match="already a curated source"):
        sources.load_sources(load_site(clash))
    write_yaml(tmp_path / "d" / "site.yaml", {"folio": {"base_url": "https://f.example", "tenant": "t",
                                                        "username": "u"}, "recipe_sources": {"id": "x"}})
    with pytest.raises(ConfigError, match="recipe_sources: expected a list"):
        load_site(tmp_path / "d")


def test_the_command_line_reviews_then_installs(site, monkeypatch, capsys):
    monkeypatch.setattr(sources, "_session", lambda: FakeGitHub({"recipes/chq-academic/1.1.0.yaml": newer()}))
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert main(["-s", str(site.root), "recipes", "get", "foliohq"]) == 2
    out = capsys.readouterr().out
    assert "NEW      chq-academic 1.1.0" in out and "Adds a column." in out and "Nothing installed." in out
    assert main(["-s", str(site.root), "recipes", "get", "foliohq", "--yes"]) == 0
    assert "Installed: chq-academic@1.1.0" in capsys.readouterr().out
    assert main(["-s", str(site.root), "recipes", "sources"]) == 0
    assert f"downloaded at commit {SHA[:12]}" in capsys.readouterr().out


# ------------------------------------------------------------------ review findings
def test_a_minor_version_that_changes_what_feeds_send_is_refused(site):
    from sitekit import make_feed

    make_feed(site.root)                                            # chq on chq-academic@^1.0
    renamed = newer("1.1.0", "Fix a typo.").replace(b"{header: Item record ID,", b"{header: Item ID,", 1)
    assert renamed != newer("1.1.0", "Fix a typo.")
    result = sources.check(site, lib(site), "foliohq", http=FakeGitHub({"recipes/chq-academic/1.1.0.yaml": renamed}))
    (item,) = result["recipes"]
    assert item["status"] == "invalid" and "changes outputs" in item["problem"] and "2.0.0" in item["problem"]
    assert result["new"] == []
    wording = newer("1.1.0", "Clearer description.").replace(b"description: >", b"description: >\n  Clearer.", 1)
    result = sources.check(site, lib(site), "foliohq", http=FakeGitHub({"recipes/chq-academic/1.1.0.yaml": wording}))
    assert result["new"] == ["chq-academic@1.1.0"] and result["recipes"][0]["picked_up_by"] == ["chq"]


def test_redirects_are_followed_only_within_github(site):
    class Moved(FakeGitHub):
        def get(self, url, timeout=None, headers=None, allow_redirects=True):
            assert allow_redirects is False
            if url.endswith("/commits/main") and "renamed" not in url:
                return self._resp(301, b"", url, {"Location": url.replace("FOLIOHQ", "FOLIOHQ-renamed")})
            return super().get(url.replace("FOLIOHQ-renamed", "FOLIOHQ"), timeout, headers, allow_redirects)

    assert sources.check(site, lib(site), "foliohq", http=Moved({}))["commit"] == SHA

    class Away(FakeGitHub):
        def get(self, url, timeout=None, headers=None, allow_redirects=True):
            return self._resp(302, b"", url, {"Location": "https://evil.example/recipes"})

    with pytest.raises(FxError, match="Refused to download from evil.example"):
        sources.check(site, lib(site), "foliohq", http=Away({}))


def test_downloads_never_go_into_the_program_folder(tmp_path):
    root = make_site(tmp_path / "s", "https://folio.example.org", paths={"recipes": []})
    s = load_site(root)
    with pytest.raises(ConfigError, match="this site's own recipes folder"):
        sources.install(s, lib(s), "foliohq", SHA, http=FakeGitHub({"recipes/chq-academic/1.1.0.yaml": newer()}))
    assert not (BUNDLED_RECIPES / "chq-academic" / "1.1.0.yaml").exists()
    assert sources.provenance(s) == {}


def test_odd_file_names_in_the_tree_are_ignored(site):
    gh = FakeGitHub({"recipes/chq-academic/1.1.0.yaml": newer(), "recipes/chq-academic/1.1.0.yaml\n": newer()})
    assert [r["path"] for r in sources.check(site, lib(site), "foliohq", http=gh)["recipes"]] == \
        ["recipes/chq-academic/1.1.0.yaml"]


def test_an_emptied_sources_list_is_just_empty(tmp_path):
    root = make_site(tmp_path / "s", "https://folio.example.org")
    (root / "site.yaml").write_text((root / "site.yaml").read_text(encoding="utf-8") + "recipe_sources:\n",
                                    encoding="utf-8")
    assert [s.id for s in sources.load_sources(load_site(root))] == ["foliohq"]


def test_a_windows_checkout_with_crlf_line_endings_is_the_same_recipe(site, tmp_path):
    crlf = tmp_path / "crlf"
    (crlf / "chq-academic").mkdir(parents=True)
    (crlf / "chq-academic" / "1.0.0.yaml").write_bytes(BUNDLED.replace(b"\n", b"\r\n"))
    crlf_library = RecipeLibrary([crlf])
    result = sources.check(site, crlf_library, "foliohq", http=FakeGitHub({"recipes/chq-academic/1.0.0.yaml": BUNDLED}))
    assert result["recipes"][0]["status"] == "installed"
