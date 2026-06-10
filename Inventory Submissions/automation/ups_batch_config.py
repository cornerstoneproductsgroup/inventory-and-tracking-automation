"""Paths and settings for UPS.com batch file shipping (Home Depot lane)."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from automation.pull_orders_config import date_stamp


def _p(env_key: str, default: str) -> Path:
    return Path((os.environ.get(env_key) or default).strip())


_PACKING_SLIPS = _p(
    "UPS_PACKING_SLIPS_BASE",
    r"\\rygarcorp.com\shares\Cornerstone\Dot Com Packing Slips",
)

DEPOT_CSV_OUTPUT_DIR = _p(
    "UPS_DEPOT_CSV_DIR",
    str(
        _PACKING_SLIPS
        / "1-Orders Before Extraction"
        / "Order Splitter Output"
        / "CSV File Output"
        / "Depot"
    ),
)

DEPOT_LABELS_MAIN_DIR = _p(
    "UPS_DEPOT_LABELS_MAIN_DIR",
    str(
        _PACKING_SLIPS
        / "2-Home Depot"
        / "1 - UPS Shipping Labels"
        / "1 - Main File For The Day"
    ),
)

_INVENTORY_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_HOME_URL = (
    os.environ.get("UPS_HOME_URL") or "https://www.ups.com/us/en/home"
).strip()

DEFAULT_BATCH_LANDING_URL = (
    os.environ.get("UPS_BATCH_LANDING_URL")
    or "https://www.ups.com/us/en/shipping/batch-file-shipping"
).strip()

STORAGE_STATE = _INVENTORY_ROOT / (
    (os.environ.get("UPS_STORAGE_STATE") or "ups_storage_state.json").strip()
)

DEFAULT_BROWSER_PROFILE_DIR = _INVENTORY_ROOT / "ups_browser_profile"


def depot_output_basename(order_date: date | None = None) -> str:
    """e.g. Depot 6-10-2026 Output.csv"""
    d = order_date or date.today()
    return f"Depot {date_stamp(d)} Output.csv"


def depot_output_path(order_date: date | None = None) -> Path:
    return DEPOT_CSV_OUTPUT_DIR / depot_output_basename(order_date)


def depot_labels_pdf_name(order_date: date | None = None) -> str:
    """e.g. Home Depot 6-10-2026 Labels.pdf"""
    d = order_date or date.today()
    return f"Home Depot {date_stamp(d)} Labels.pdf"


def depot_labels_pdf_path(order_date: date | None = None) -> Path:
    return DEPOT_LABELS_MAIN_DIR / depot_labels_pdf_name(order_date)


def label_save_timeout_s() -> float:
    raw = (os.environ.get("UPS_LABEL_SAVE_TIMEOUT_S") or "180").strip()
    try:
        return max(30.0, float(raw))
    except ValueError:
        return 180.0
