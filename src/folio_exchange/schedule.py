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
# A scheduled run with one of these statuses settles its slot: it happened, or it needs a person rather
# than a retry. A run started by hand settles the slot only if it delivered (status success); building,
# previews and failures by hand never stop the scheduled delivery.
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
    t = token.strip().lower()
    full = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    for i, name in enumerate(full):
        if len(t) >= 3 and name.startswith(t):
            return i
    raise ConfigError(f"schedule {original!r}: {token!r} is not a weekday (Mon to Sun). Use {FORMS}")


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
    s: Schedule, now: datetime, tz: ZoneInfo | None, runs: list[dict], max_attempts: int,
    created: datetime | None,
) -> bool:
    """True when the latest slot has passed and no run since has settled it, and fewer than ``max_attempts``
    scheduled runs have started since it (failed runs are retried).

    ``runs`` are the feed's runs started at or after the slot. ``created`` is when the site first saw the
    feed: slots before it are never due (None: no such rule)."""
    slot = last_slot(s, now, tz)
    if slot is None or (created is not None and slot <= created):
        return False
    since = [r for r in runs if _started(r) is not None and _started(r) >= slot]
    scheduled = [r for r in since if r.get("trigger") == "schedule"]
    if any((r.get("report") or {}).get("no_retry") for r in scheduled):
        return False                         # FOLIO refused the login: retrying could lock the account
    if any(r.get("status") in SETTLED for r in scheduled) or any(r.get("status") == "success" and
                                                                  r.get("trigger") != "preview" for r in since):
        return False
    return len(scheduled) < max(1, max_attempts)


# ------------------------------------------------------------- OS scheduler
def task_name(site_root: Path) -> str:
    root = Path(site_root)
    label = root.name or str(root).rstrip("\\/:") or "site"      # a drive root such as C:\ has no name
    return f"FOLIO-Exchange ({label})".replace('"', "")


def win_quote(arg: str) -> str:
    """One argument in double quotes, escaped by the Windows C runtime rules: a quote becomes \\", and
    backslashes are doubled where they come before a quote (so C:\\ stays C:\\ rather than escaping it)."""
    out, slashes = "", 0
    for c in arg:
        if c == "\\":
            slashes += 1
            continue
        out += "\\" * (slashes * 2 + 1) + c if c == '"' else "\\" * slashes + c
        slashes = 0
    return f'"{out}{chr(92) * slashes * 2}"'


def os_command(site_root: Path, executable: str, system: str, user: str | None = None, *,
               shell: str = "cmd") -> str:
    """The command that registers an hourly ``run --due`` with the OS scheduler. Shown, never run.

    On Windows the task runs as ``user`` whether or not anyone is logged on (schtasks asks for that
    user's Windows password), because secrets in Credential Manager belong to the user who stored them.
    ``shell="powershell"`` hands the line to Command Prompt with ``cmd --% /c``, which reaches schtasks
    unchanged from Windows PowerShell 5.1 and PowerShell 7 alike. (``schtasks --% ...`` does not: since 7.3,
    PowerShell re-quotes the text after ``--%`` for most programs, which breaks the escaped quotes in /TR.)"""
    site = str(Path(site_root).resolve())
    exe = executable or "folio-exchange"
    if system.lower().startswith("win"):
        action = f"{win_quote(exe.replace(chr(34), ''))} --site {win_quote(site)} run --due"
        who = f' /RU "{user}" /RP' if user else ""
        via = "cmd --% /c " if shell == "powershell" else ""
        return (f'{via}schtasks /Create /TN "{task_name(site)}" /SC HOURLY /MO 1 /ST 00:07 '
                f"/TR {win_quote(action)}{who} /F")
    q = lambda v: "'" + v.replace("'", "'\"'\"'") + "'"
    log = str(Path(site) / "logs" / "cron.log")
    return f"7 * * * * {q(exe)} --site {q(site)} run --due >> {q(log)} 2>&1"


TASK_TIME_LIMIT = "PT12H"      # a pass still going after this is stopped (Task Scheduler's own default is 72 hours)


def windows_task_action(site_root: Path, executable: str) -> tuple[str, str]:
    """The program and the arguments the task starts: ``run --due`` for this site."""
    site = str(Path(site_root).resolve())
    return win_quote(executable.replace(chr(34), "")), f"--site {win_quote(site)} run --due"


def windows_task_xml(site_root: Path, executable: str, user: str | None = None) -> str:
    """The task, in Task Scheduler's XML: every hour at minute 7, as ``user`` whether or not anyone is signed in.
    Unlike schtasks' own defaults it starts and keeps running on battery power, starts as soon as it can after a
    missed start (the computer was off or asleep), never starts twice at once, and stops after TASK_TIME_LIMIT."""
    from xml.sax.saxutils import escape

    command, arguments = windows_task_action(site_root, executable)
    who = f"\n      <UserId>{escape(user)}</UserId>" if user else ""
    logon = "Password" if user else "InteractiveToken"
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>FOLIO-Exchange: runs the feeds that are due ({escape(str(Path(site_root).resolve()))})</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>2020-01-01T00:07:00</StartBoundary>
      <Repetition>
        <Interval>PT1H</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">{who}
      <LogonType>{logon}</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>{TASK_TIME_LIMIT}</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(command)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def windows_task_args(site_root: Path, xml_file: Path | str, user: str | None = None) -> list[str]:
    """The schtasks command that registers the task from ``windows_task_xml`` saved in ``xml_file`` (UTF-16), as
    arguments for running it directly (no shell). With a user, schtasks itself asks for that user's Windows
    password, once."""
    args = ["schtasks", "/Create", "/TN", task_name(Path(site_root).resolve()), "/XML", str(xml_file)]
    return args + (["/RU", user, "/RP", "*"] if user else []) + ["/F"]


def os_helpers(site_root: Path, system: str) -> dict[str, str]:
    """Commands to run the task now, see its last result, and remove it (Windows); empty elsewhere."""
    if not system.lower().startswith("win"):
        return {"list": "crontab -l", "edit": "crontab -e"}
    name = task_name(site_root)
    return {"run now": f'schtasks /Run /TN "{name}"', "last result": f'schtasks /Query /TN "{name}" /V /FO LIST',
            "remove": f'schtasks /Delete /TN "{name}" /F'}
