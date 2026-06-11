"""Resolve Home Depot Order Splitter Output CSV for UPS.com batch upload."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from automation.ups_lane_csv import (
    DepotCsvSkip,
    UpsCsvSkip,
    list_output_csvs,
    resolve_upload_csv as _resolve_upload_csv,
)

__all__ = ("DepotCsvSkip", "UpsCsvSkip", "list_output_csvs", "resolve_upload_csv")


def resolve_upload_csv(
    *,
    order_date: date | None = None,
    explicit_path: Path | str | None = None,
) -> Path:
    return _resolve_upload_csv(
        lane="depot",
        order_date=order_date,
        explicit_path=explicit_path,
    )
