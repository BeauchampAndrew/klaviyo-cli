"""Shared helpers for command modules: timezone parsing, date ranges, output."""

import json as json_module
from datetime import datetime

import click
from zoneinfo import ZoneInfo

TZ_MAP = {
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    "MST": "America/Denver",
    "MDT": "America/Denver",
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
}


def _parse_send_time(date_str: str, time_str: str) -> str:
    """Parse MM-DD-YYYY + 'HH:MM AM/PM TZ' into an ISO 8601 string with timezone."""
    # Normalize date from MM-DD-YYYY → YYYY-MM-DD
    parts = date_str.strip().split("-")
    if len(parts) == 3 and len(parts[2]) == 4:
        # MM-DD-YYYY
        month, day, year = parts
        iso_date = f"{year}-{month}-{day}"
    else:
        # Assume already YYYY-MM-DD
        iso_date = date_str.strip()

    time_parts = time_str.strip().split()
    tz_name = "America/New_York"  # default

    if len(time_parts) == 3:
        # "3:00 PM EST"
        t, ampm, tz_abbr = time_parts
        t_str = f"{t} {ampm}"
        fmt = "%I:%M %p"
        tz_name = TZ_MAP.get(tz_abbr.upper(), tz_abbr)
    elif len(time_parts) == 2:
        if time_parts[1].upper() in ("AM", "PM"):
            # "3:00 PM"
            t_str = time_str.strip()
            fmt = "%I:%M %p"
        else:
            # "15:00 CST"
            t_str = time_parts[0]
            fmt = "%H:%M"
            tz_name = TZ_MAP.get(time_parts[1].upper(), time_parts[1])
    else:
        # "15:00"
        t_str = time_parts[0]
        fmt = "%H:%M"

    dt = datetime.strptime(f"{iso_date} {t_str}", f"%Y-%m-%d {fmt}")
    dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return dt.isoformat()


def _resolve_date_range(days: int, since: str | None, until: str | None):
    """Resolve a (start, end, label) tuple from --days / --since / --until.

    If either --since or --until is provided, the explicit bounds win. Otherwise
    falls back to (now - days) .. now. Dates are parsed as YYYY-MM-DD in UTC; the
    end date is inclusive (23:59:59).
    """
    from datetime import datetime, timedelta, timezone

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    label = f"last {days} days"

    if since or until:
        if since:
            try:
                start = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                raise click.ClickException(f"Invalid --since date {since!r} — expected YYYY-MM-DD")
        if until:
            try:
                parsed = datetime.strptime(until, "%Y-%m-%d")
                end = parsed.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
            except ValueError:
                raise click.ClickException(f"Invalid --until date {until!r} — expected YYYY-MM-DD")
        label = f"{start.strftime('%Y-%m-%d')} to {end.strftime('%Y-%m-%d')}"

    return start, end, label


def output(data, use_json: bool = False):
    """Print data — raw JSON if use_json, otherwise assume caller formatted it."""
    if use_json:
        print(json_module.dumps(data, indent=2, default=str))
    else:
        if isinstance(data, str):
            print(data)
        else:
            # Fallback for unformatted data — callers should format before calling
            print(json_module.dumps(data, indent=2, default=str))
