"""Previous U.S. business day for daily invoice pulls (Mon→Fri, Tue–Fri→yesterday, weekend→last Friday)."""

from __future__ import annotations

import datetime as dt


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


def format_criteria_datetime(d: dt.date, *, end_of_day: bool) -> str:
    """MM/DD/YYYY h:mm:ss AM/PM as used by CommerceHub criteria fields."""
    if end_of_day:
        t = dt.datetime.combine(d, dt.time(23, 0, 0))
    else:
        t = dt.datetime.combine(d, dt.time(0, 0, 0))
    return t.strftime("%m/%d/%Y %I:%M:%S %p")
