"""Previous U.S. business day for daily invoice pulls (Mon→Fri, Tue–Fri→yesterday, weekend→last Friday)."""

from __future__ import annotations

import datetime as dt
import re


def previous_business_day(today: dt.date | None = None) -> dt.date:
    """
    CommerceHub rule from ops:
    - Tue–Fri: calendar day before `today`
    - Monday: preceding Friday
    - Saturday / Sunday: preceding Friday (for jobs that still run on weekends)
    """
    d = today or dt.date.today()
    wd = d.weekday()  # Mon=0 .. Sun=6

    if wd == 0:  # Monday
        return d - dt.timedelta(days=3)
    if wd == 5:  # Saturday
        return d - dt.timedelta(days=1)
    if wd == 6:  # Sunday
        return d - dt.timedelta(days=2)
    return d - dt.timedelta(days=1)


def parse_report_date(value: str) -> dt.date:
    """Parse a user-supplied invoice report date (YYYY-MM-DD, MM/DD/YYYY, M/D/YYYY, etc.)."""
    s = (value or "").strip()
    if not s:
        raise ValueError("Invoice report date is empty.")
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    m = re.fullmatch(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})", s)
    if m:
        month, day, year = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return dt.date(year, month, day)
    raise ValueError(
        f"Invalid invoice report date {value!r}; use YYYY-MM-DD or MM/DD/YYYY (e.g. 2026-05-23 or 5/23/2026)."
    )


def format_criteria_datetime(d: dt.date, *, end_of_day: bool) -> str:
    """MM/DD/YYYY h:mm:ss AM/PM as used by CommerceHub criteria fields."""
    if end_of_day:
        t = dt.datetime.combine(d, dt.time(23, 0, 0))
    else:
        t = dt.datetime.combine(d, dt.time(0, 0, 0))
    return t.strftime("%m/%d/%Y %I:%M:%S %p")
