"""WorldShip UPS_CSV_EXPORT — PO (column A) and tracking (column B).

Used for CommerceHub Depot + Special Order tracking and SPS Tractor + Grainger.
"""

from __future__ import annotations

import csv
import os
import re
from collections.abc import Iterator
from pathlib import Path

UPS_TRACKING_DIR = Path(
    r"\\rygarcorp.com\shares\Cornerstone\Dot Com Packing Slips\1-Orders Before Extraction"
    r"\Order Splitter Output\z - UPS Tracking"
)
UPS_TRACKING_BASENAME = "UPS_CSV_EXPORT"
PO_COLUMN_INDEX = 0
TRACKING_COLUMN_INDEX = 1


def resolve_ups_tracking_csv_path() -> Path:
    """Path to WorldShip batch export (UPS_TRACKING_CSV_PATH in .env overrides)."""
    explicit = (os.environ.get("UPS_TRACKING_CSV_PATH") or "").strip()
    if explicit:
        return Path(explicit)
    for name in (
        f"{UPS_TRACKING_BASENAME}.csv",
        f"{UPS_TRACKING_BASENAME}.CSV",
        UPS_TRACKING_BASENAME,
    ):
        candidate = UPS_TRACKING_DIR / name
        if candidate.is_file():
            return candidate
    return UPS_TRACKING_DIR / f"{UPS_TRACKING_BASENAME}.csv"


def _read_csv_rows(path: Path) -> list[list[str]]:
    if not path.is_file():
        return []
    for enc in ("utf-8-sig", "latin1"):
        try:
            with path.open("r", newline="", encoding=enc) as handle:
                return list(csv.reader(handle))
        except UnicodeDecodeError:
            continue
    return []


def _looks_like_ups_tracking(value: str) -> bool:
    text = (value or "").strip().upper().replace(" ", "")
    return len(text) >= 10 and text.startswith("1Z")


def _row_looks_like_header(po: str, tracking: str) -> bool:
    po_l = (po or "").strip().lower()
    track_l = (tracking or "").strip().lower()
    if not po_l and not track_l:
        return True
    if "po" in po_l and not re.search(r"\d{4,}", po_l):
        return True
    if "track" in track_l and not _looks_like_ups_tracking(tracking):
        return True
    if po_l in ("po#", "po", "purchase order", "referencenumber1"):
        return True
    return False


def iter_po_tracking_rows(path: Path | str) -> Iterator[tuple[str, str]]:
    """Yield (po_raw, tracking) from column A and B; skips header row when detected."""
    rows = _read_csv_rows(Path(path))
    if not rows:
        return

    start = 0
    if len(rows[0]) > TRACKING_COLUMN_INDEX:
        po0 = (rows[0][PO_COLUMN_INDEX] or "").strip()
        tr0 = (rows[0][TRACKING_COLUMN_INDEX] or "").strip()
        if _row_looks_like_header(po0, tr0):
            start = 1

    for row in rows[start:]:
        if len(row) <= TRACKING_COLUMN_INDEX:
            continue
        po_raw = (row[PO_COLUMN_INDEX] or "").strip()
        tracking = (row[TRACKING_COLUMN_INDEX] or "").strip().split()[0]
        if not po_raw or not tracking:
            continue
        if _row_looks_like_header(po_raw, tracking):
            continue
        yield po_raw, tracking
