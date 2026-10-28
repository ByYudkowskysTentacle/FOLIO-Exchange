"""Date helpers. Internally every timestamp is a UTC string 'YYYY-MM-DDTHH:MM:SS'
so that SQLite string comparison and MAX() order them correctly."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dateutil import parser as dtparser

FMT = "%Y-%m-%dT%H:%M:%S"
EARLIEST_LEGACY_YEAR = 1800
# A leading label on a migrated note, e.g. "Created: " or "Checkouts: " (a letter first, no digits).
LABEL = re.compile(r"^[^\W\d_][^\d:]*:\s*")
# Two different defaults: a part that differs between the two results was missing from the text.
_DEFAULTS = (datetime(2001, 1, 1), datetime(2002, 2, 2))


def _iso(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None


def _utc(dt: datetime, naive_tz=None) -> datetime | None:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=naive_tz or timezone.utc)
    try:  # year 1 east of UTC, or 9999-12-31 late in the day west of it, has no UTC equivalent
        return dt.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def parse(value: str | None, naive_tz=None) -> datetime | None:
    """Parse to UTC. Values without an offset are taken as ``naive_tz`` (default UTC)."""
    if not value:
        return None
    text = str(value).strip()
    dt = _iso(text)  # fast path: FOLIO timestamps are ISO 8601
    if dt is None:
        try:  # free text, e.g. "3/15/1998"
            dt = dtparser.parse(text)
        except (ValueError, OverflowError):
            return None
    return _utc(dt, naive_tz)


def norm(value: str | None, naive_tz=None) -> str | None:
    dt = parse(value, naive_tz)
    return dt.strftime(FMT) if dt else None


def _whole_date(text: str) -> datetime | None:
    """Free text with a day, month and year (never filled in from today), else None."""
    try:
        a, b = (dtparser.parse(text, default=d) for d in _DEFAULTS)
    except (ValueError, OverflowError):
        return None
    return a if a == b else None


def legacy(value: str, naive_tz, date_format: str | None = None, now: datetime | None = None) -> str | None:
    """A migrated date note as a UTC string, or None when it is not a whole, plausible date.

    Accepts ISO 8601, then ``date_format`` when given, else free text with a day, month and year.
    A leading label such as "Created: " is ignored, two-digit years fall in the past, and dates
    before 1800 or after tomorrow are rejected. A ``date_format`` that starts with the label itself
    ("Created: %d/%m/%Y") reads notes with that label, with another one or with none."""
    raw = str(value).strip()
    text = LABEL.sub("", raw, count=1)
    dt = _iso(text)
    two_digit = False
    if dt is None and date_format:
        bare = LABEL.sub("", date_format, count=1)
        for note, pattern in dict.fromkeys([(raw, date_format), (text, date_format), (text, bare)]):
            try:
                dt = datetime.strptime(note, pattern)
                break
            except ValueError:
                continue
        if dt is None:
            return None
        two_digit = "%y" in date_format
    elif dt is None:
        dt = _whole_date(text)
        two_digit = dt is not None and str(dt.year) not in text
    if dt is None:
        return None
    now = now or now_utc()
    utc = _utc(dt, naive_tz)
    if two_digit and utc is not None and utc > now:
        try:
            dt = dt.replace(year=dt.year - 100)
        except ValueError:  # 29 February
            return None
        utc = _utc(dt, naive_tz)
    if utc is None or dt.year < EARLIEST_LEGACY_YEAR or utc > now + timedelta(days=1):
        return None
    return utc.strftime(FMT)


def legacy_format_problem(pattern: object) -> str | None:
    """Why ``pattern`` cannot read legacy date notes, or None when it can."""
    if not isinstance(pattern, str) or not pattern.strip():
        return f"must be a date pattern such as %d/%m/%Y, got {pattern!r}"
    parts = (("day", "d"), ("month", "mbB"), ("year", "Yy"))
    missing = [name for name, codes in parts if not any(f"%{c}" in pattern for c in codes)]
    if missing:
        return f"{pattern!r} has no {' or '.join(missing)}; use a pattern such as %d/%m/%Y"
    sample = datetime(1998, 3, 15)
    try:
        ok = datetime.strptime(sample.strftime(pattern), pattern).date() == sample.date()
    except ValueError:
        ok = False
    return None if ok else f"{pattern!r} cannot read dates back; use a pattern such as %d/%m/%Y"


def to_folio(dt: datetime) -> str:
    """Format for CQL queries against metadata.updatedDate."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def fmt(value: str | None, out_format: str, tz: ZoneInfo | None) -> str:
    dt = parse(value)
    if dt is None:
        return ""
    if tz is not None:
        try:
            dt = dt.astimezone(tz)
        except (OverflowError, ValueError):
            return ""
    return dt.strftime(out_format)


def latest(*values: str | None) -> str | None:
    vals = [v for v in values if v]
    return max(vals) if vals else None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
