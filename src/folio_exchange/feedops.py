"""Feed operations shared by the command line and the local page, so the two never drift apart.

Pausing and resuming work on the feed file itself, so even a feed whose file has a problem (the one most
likely to need pausing) can be paused. Site and feed warnings are returned as data; each front end only
words and shows them.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from . import runner
from .config import read_yaml, rewrite_key
from .errors import ConfigError
from .history import History

if TYPE_CHECKING:
    from .config import Site
    from .recipe import Feed, RecipeLibrary


def set_enabled(site: Site, name: str, enabled: bool, *, via: str = "cli") -> dict:
    """Pause (``enabled=False``) or resume a feed by rewriting its ``enabled:`` line, keeping comments and
    layout. Works when the file does not load. Returns {name, enabled, changed}."""
    path = site.feeds_dir / f"{name}.yaml"
    if not path.is_file():
        raise ConfigError(f"No feed file {path}")
    try:
        current = read_yaml(path).get("enabled", True)
    except ConfigError:
        current = None                         # the file has a problem: set the line anyway
    if current is enabled:
        return {"name": name, "enabled": enabled, "changed": False}
    rewrite_key(path, "enabled", "true" if enabled else "false")
    action = "feed resumed" if enabled else "feed paused"
    with History(site.history_path) as hist:
        hist.audit(None, name, action, {"file": str(path), "from": via})
        runner.audit_settings(site, hist)
    return {"name": name, "enabled": enabled, "changed": True}


def _w(level: str, code: str, text: str, feed: str | None = None) -> dict:
    return {"level": level, "code": code, "text": text, "feed": feed}


def site_warnings(site: Site, library: RecipeLibrary, feeds: list[Feed], errors: dict[str, str], hist: History,
                  now: datetime, *, only: str | None = None) -> list[dict]:
    """Everything a person should know before trusting the site to run on its own, most serious first.

    Each item is {level: problem | warning | note, code, text, feed}. ``only`` limits feed items to one feed."""
    out: list[dict] = []
    for name, err in errors.items():
        if not only or name == only:
            out.append(_w("problem", "feed-file", err, name))
    for problem in library.problems():
        out.append(_w("problem", "recipe-file", problem))
    if not site.alerts.get("webhook") and not site.alerts.get("email"):
        out.append(_w("warning", "no-alerts", "No alert channel is set up, so a failed scheduled run would go "
                      "unnoticed. Add alerts.webhook or alerts.email to site.yaml, then run "
                      "`folio-exchange check --alerts`."))
    elif site.alerts.get("on") == "never":
        out.append(_w("warning", "alerts-never", "alerts.on is never: failed runs send no alert."))
    for f in feeds:
        if only and f.name != only:
            continue
        if f.delivery.get("host_key_confirmed") is False:
            out.append(_w("warning", "host-key-unconfirmed",
                          f"The SFTP server key of {f.delivery.get('host')} is not confirmed with the vendor yet, "
                          "so nothing logs in to it. Run `folio-exchange init` again once the vendor confirms it.",
                          f.name))
        if f.delivers and not f.checks.get("min_item_ratio"):
            out.append(_w("warning", "ratio-off", "checks.min_item_ratio is 0, so a sudden drop in items would be "
                          "delivered.", f.name))
        if f.enabled:
            late = runner.overdue(site, f, hist, now)
            if late:
                out.append(_w("warning", "overdue", late[0].upper() + late[1:] + ".", f.name))
        else:
            out.append(_w("note", "paused", f"Paused: scheduled runs skip it. Test it with `folio-exchange check "
                          f"--delivery {f.name}`, then `folio-exchange feeds resume {f.name}`.", f.name))
        newer = library.newer_major(f.uses)
        if newer:
            out.append(_w("note", "recipe-update", f"{newer.ref} is available and waits for review: "
                          f"folio-exchange upgrade {f.name} --to {newer.id}@^{newer.version[0]}.{newer.version[1]}",
                          f.name))
    order = {"problem": 0, "warning": 1, "note": 2}
    return sorted(out, key=lambda w: order[w["level"]])
