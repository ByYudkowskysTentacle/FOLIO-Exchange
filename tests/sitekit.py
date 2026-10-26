"""Build throwaway site folders for tests: site.yaml plus feeds/<name>.yaml, as plain YAML files.

Nothing here imports folio_exchange, so the helpers stay valid while the loaders change.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

FOLIO_TEST_PASSWORD = "secret"   # what tests/mock_folio.py accepts


def recent(days: float) -> str:
    """A UTC timestamp `days` ago, in the form FOLIO returns."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out


def write_yaml(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def site_data(folio_url: str, **overrides: dict) -> dict:
    data = {
        "folio": {"base_url": folio_url, "tenant": "diku", "username": "fx_reader", "page_size": 2},
        "sync": {"mode": "auto", "full_refresh_days": 30},
        "alerts": {"on": "never"},
    }
    return merge(data, overrides)


def make_site(root: Path, folio_url: str, **overrides: dict) -> Path:
    """Write root/site.yaml and return root. The password comes from FOLIO_PASSWORD (set it in the test)."""
    root.mkdir(parents=True, exist_ok=True)
    write_yaml(root / "site.yaml", site_data(folio_url, **overrides))
    return root


def feed_data(root: Path, uses: str = "chq-academic@^1.0", **overrides: Any) -> dict:
    data = {
        "uses": uses,
        "file_prefix": "test",
        "filters": {"exclude_location_codes": ["ONLINE"]},
        "legacy": {"circ_count_note_type": "Legacy circ count", "created_date_note_type": "Legacy created date"},
        "delivery": {"transport": "local", "directory": str(root / "vendor-drop")},
        "schedule": "Sun 02:15",
    }
    return merge(data, overrides)


def make_feed(root: Path, name: str = "chq", uses: str = "chq-academic@^1.0", **overrides: Any) -> Path:
    """Write root/feeds/<name>.yaml (local settings over a shared recipe) and return its path."""
    return write_yaml(root / "feeds" / f"{name}.yaml", feed_data(root, uses, **overrides))
