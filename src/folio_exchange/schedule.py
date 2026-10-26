"""Feed schedules: parsing, slots, and whether a feed is due.

Nothing stays running. The OS scheduler starts ``folio-exchange run --due`` every hour,
and a feed runs when its latest slot has passed and no run has dealt with that slot.
A PC that was off for weeks runs each feed once at the next start, not once per
missed slot.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .errors import ConfigError

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_NAMES = ("Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays")
DEFAULT_TIME = (2, 0)
# Statuses that settle a slot: the run happened, or it needs a person rather than a retry.
SETTLED = {"success", "built", "blocked", "held"}

FORMS = (
    "daily | weekly | monthly | manual, or e.g. 'daily 03:30', 'Sun 02:15', 'Mon,Thu 06:00', 'monthly 15 01:00'"
)


@dataclass(frozen=True)
class Schedule:
    kind: str                 # daily | weekly | monthly | manual
    days: tuple[int, ...]     # weekdays 0=Mon..6=Sun (weekly) or (day of month,) (monthly)
    hour: int
    minute: int
    text: str


def _hhmm(text: str, original: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ConfigError(f"schedule {original!r}: {text!r} is not a time like 02:15. Use {FORMS}")
    return int(m.group(1)), int(m.group(2))


def _day(token: str, original: str) -> int:
    t = token.strip().lower()[:3]
    if t not in DAYS:
        raise ConfigError(f"schedule {original!r}: {token!r} is not a weekday (Mon to Sun). Use {FORMS}")
    return DAYS.index(t)


def parse(text: str) -> Schedule:
    original = str(text or "").strip()
    words = original.split()
    if not words:
        raise ConfigError(f"schedule is empty. Use {FORMS}")
    head = words[0].lower()
    if head == "manual" and len(words) == 1:
        return Schedule("manual", (), 0, 0, original)
    if head == "daily" and len(words) <= 2:
        h, m = _hhmm(words[1], original) if len(words) == 2 else DEFAULT_TIME
        return Schedule("daily", tuple(range(7)), h, m, original)
    if head == "weekly" and len(words) <= 2:
        h, m = _hhmm(words[1], original) if len(words) == 2 else DEFAULT_TIME
        return Schedule("weekly", (6,), h, m, original)
    if head == "monthly" and len(words) in (1, 3):
        if len(words) == 1:
            return Schedule("monthly", (1,), *DEFAULT_TIME, original)
        if not words[1].isdigit() or not 1 <= int(words[1]) <= 28:
            raise ConfigError(f"schedule {original!r}: the day of the month must be 1 to 28. Use {FORMS}")
        return Schedule("monthly", (int(words[1]),), *_hhmm(words[2], original), original)
    if len(words) == 2:
        days = tuple(sorted({_day(t, original) for t in words[0].split(",") if t.strip()}))
        if days:
            return Schedule("weekly", days, *_hhmm(words[1], original), original)
    raise ConfigError(f"schedule {original!r} is not understood. Use {FORMS}")


# ------------------------------------------------------------------ slots
def _aware(d: date, s: Schedule, tz: ZoneInfo | None) -> datetime:
    """The slot's wall-clock time on day ``d`` in ``tz`` (None = this computer's local time), in UTC."""
    naive = datetime.combine(d, time(s.hour, s.minute))
    local = naive.replace(tzinfo=tz) if tz is not None else naive.astimezone()
    return local.astimezone(timezone.utc)


def _local_today(now: datetime, tz: ZoneInfo | None) -> date:
    return (now.astimezone(tz) if tz is not None else now.astimezone()).date()


def _on(s: Schedule, d: date) -> bool:
    if s.kind == "monthly":
        return d.day == s.days[0]
    return d.weekday() in s.days


def last_slot(s: Schedule, now: datetime, tz: ZoneInfo | None) -> datetime | None:
    """The latest slot at or before ``now`` (aware, UTC), or None for manual schedules."""
    if s.kind == "manual":
        return None
    d = _local_today(now, tz)
    for _ in range(62):
        if _on(s, d):
            slot = _aware(d, s, tz)
            if slot <= now:
                return slot
        d -= timedelta(days=1)
    return None


def next_slot(s: Schedule, now: datetime, tz: ZoneInfo | None) -> datetime | None:
    """The first slot after ``now`` (aware, UTC), or None for manual schedules."""
    if s.kind == "manual":
        return None
    d = _local_today(now, tz) - timedelta(days=1)
    for _ in range(64):
        if _on(s, d):
            slot = _aware(d, s, tz)
            if slot > now:
                return slot
        d += timedelta(days=1)
    return None


def describe(s: Schedule) -> str:
    at = f"{s.hour:02d}:{s.minute:02d}"
    if s.kind == "manual":
        return "only when started by hand"
    if s.kind == "daily" or (s.kind == "weekly" and len(s.days) == 7):
        return f"every day at {at}"
    if s.kind == "monthly":
        return f"on day {s.days[0]} of each month at {at}"
    names = [DAY_NAMES[d] for d in s.days]
    joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return f"{joined} at {at}"


def _started(run: dict) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(run.get("started")))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_due(
    s: Schedule, now: datetime, tz: ZoneInfo | None, runs: list[dict], max_attempts: int, created: datetime
) -> bool:
    """True when the latest slot has passed, the feed existed by then, no run since has settled it,
    and fewer than ``max_attempts`` runs have started since it (failed runs are retried)."""
    slot = last_slot(s, now, tz)
    if slot is None or slot <= created:
        return False
    since = [r for r in runs if (_started(r) or slot) >= slot and _started(r) is not None]
    if any(r.get("status") in SETTLED for r in since):
        return False
    return len(since) < max(1, max_attempts)


# ------------------------------------------------------------- OS scheduler
def os_command(site_root: Path, executable: str, system: str) -> str:
    """The command that registers an hourly ``run --due`` with the OS scheduler. Shown, never run."""
    site = str(Path(site_root))
    if system.lower().startswith("win"):
        exe = executable.replace('"', "")
        name = f"FOLIO-Exchange ({Path(site).name})".replace('"', "")
        action = f'\\"{exe}\\" --site \\"{site}\\" run --due'
        return f'schtasks /Create /TN "{name}" /SC HOURLY /MO 1 /TR "{action}" /F'
    q = lambda v: "'" + v.replace("'", "'\"'\"'") + "'"
    log = str(Path(site) / "logs" / "cron.log")
    return f"7 * * * * {q(executable)} --site {q(site)} run --due >> {q(log)} 2>&1"
