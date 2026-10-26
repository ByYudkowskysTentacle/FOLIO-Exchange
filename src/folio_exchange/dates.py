"""Date helpers. Internally every timestamp is a UTC string 'YYYY-MM-DDTHH:MM:SS'
so that SQLite string comparison and MAX() order them correctly."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from dateutil import parser as dtparser

FMT = "%Y-%m-%dT%H:%M:%S"


def parse(value: str | None, naive_tz=None) -> datetime | None:
    """Parse to UTC. Values without an offset are taken as ``naive_tz`` (default UTC)."""
    if not value:
        return None
    text = str(value).strip()
    try:  # fast path: FOLIO timestamps are ISO 8601
        dt = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        try:  # free-text legacy values, e.g. "3/15/1998"
            dt = dtparser.parse(text)
        except (ValueError, OverflowError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=naive_tz or timezone.utc)
    return dt.astimezone(timezone.utc)


def norm(value: str | None, naive_tz=None) -> str | None:
    dt = parse(value, naive_tz)
    return dt.strftime(FMT) if dt else None


def to_folio(dt: datetime) -> str:
    """Format for CQL queries against metadata.updatedDate."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def fmt(value: str | None, out_format: str, tz: ZoneInfo | None) -> str:
    dt = parse(value)
    if dt is None:
        return ""
    if tz is not None:
        dt = dt.astimezone(tz)
    return dt.strftime(out_format)


def latest(*values: str | None) -> str | None:
    vals = [v for v in values if v]
    return max(vals) if vals else None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
