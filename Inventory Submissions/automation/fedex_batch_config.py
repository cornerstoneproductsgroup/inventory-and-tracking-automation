"""Paths and settings for FedEx batch shipping (Lowe's CSV upload + label saves)."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from automation.pull_orders_config import date_stamp


def _p(env_key: str, default: str) -> Path:
    return Path((os.environ.get(env_key) or default).strip())


_PACKING_SLIPS = _p(
    "FEDEX_PACKING_SLIPS_BASE",
    r"\\rygarcorp.com\shares\Cornerstone\Dot Com Packing Slips",
)

LOWES_LABELS_ROOT = _p(
    "FEDEX_LOWES_LABELS_DIR",
    str(_PACKING_SLIPS / "3-Lowe's" / "1-Fedex Shipping Labels"),
)

WAREHOUSE_LABEL_QUEUE_DIR = _p(
    "FEDEX_WAREHOUSE_LABEL_QUEUE_DIR",
    str(LOWES_LABELS_ROOT / "z - Warehouse Print Queue"),
)

LOWES_CSV_OUTPUT_DIR = _p(
    "FEDEX_LOWES_CSV_DIR",
    str(
        _PACKING_SLIPS
        / "1-Orders Before Extraction"
        / "Order Splitter Output"
        / "CSV File Output"
        / "Lowe's"
    ),
)

LOWES_FEDEX_MASTER_DIR = _p(
    "FEDEX_LOWES_TRACKING_DIR",
    str(
        _PACKING_SLIPS
        / "1-Orders Before Extraction"
        / "Order Splitter Output"
        / "z - Lowe's Tracking"
    ),
)

LOWES_FEDEX_MASTER_BASENAME = (
    (os.environ.get("FEDEX_LOWES_MASTER_BASENAME") or "Lowe's Fedex Master").strip()
    or "Lowe's Fedex Master"
)

DEFAULT_LOGIN_URL = "https://www.fedex.com/secure-login/en-us/"
DEFAULT_BATCH_URL = (
    os.environ.get("FEDEX_BATCH_URL") or "https://www.fedex.com/shippingplus/en-us/shipments-import"
).strip()

_INVENTORY_ROOT = Path(__file__).resolve().parent.parent

STORAGE_STATE = _INVENTORY_ROOT / (
    (os.environ.get("FEDEX_STORAGE_STATE") or "fedex_storage_state.json").strip()
)

DEFAULT_BROWSER_PROFILE_DIR = _INVENTORY_ROOT / "fedex_browser_profile"


def lowes_output_basename(order_date: date | None = None) -> str:
    """e.g. Lowe's 6-1-2026 Output.csv"""
    d = order_date or date.today()
    return f"Lowe's {date_stamp(d)} Output.csv"


def lowes_output_path(order_date: date | None = None) -> Path:
    return LOWES_CSV_OUTPUT_DIR / lowes_output_basename(order_date)


def lowes_fedex_master_path() -> Path:
    """Fixed path for Lowe's tracking automation (always overwritten)."""
    return LOWES_FEDEX_MASTER_DIR / f"{LOWES_FEDEX_MASTER_BASENAME}.xlsx"


def shipment_report_download_timeout_ms() -> int:
    raw = (os.environ.get("FEDEX_SHIPMENT_REPORT_TIMEOUT_MS") or "180000").strip()
    try:
        return max(30_000, int(raw))
    except ValueError:
        return 180_000


def _vendor_label_basename(vendor_folder: str, order_date: date | None = None) -> str:
    d = order_date or date.today()
    stamp = date_stamp(d)
    vendor = (vendor_folder or "Unknown").strip()
    name = f"Lowe's {vendor} {stamp}.pdf"
    ext = (os.environ.get("FEDEX_LABEL_EXT") or ".pdf").strip()
    if ext and not name.lower().endswith(ext.lower()):
        name = f"{name}{ext}" if ext.startswith(".") else f"{name}.{ext}"
    return name


def vendor_label_pdf_path(vendor_folder: str, order_date: date | None = None) -> Path:
    """e.g. ...\\Agra Life\\Lowe's Agra Life 6-1-2026.pdf"""
    vendor = (vendor_folder or "Unknown").strip()
    return LOWES_LABELS_ROOT / vendor / _vendor_label_basename(vendor_folder, order_date)


def warehouse_label_queue_path(vendor_folder: str, order_date: date | None = None) -> Path:
    """Staging path for warehouse Zebra labels — watcher prints when file lands here."""
    vendor = (vendor_folder or "Unknown").strip()
    return WAREHOUSE_LABEL_QUEUE_DIR / vendor / _vendor_label_basename(vendor_folder, order_date)


def upload_poll_timeout_s() -> float:
    raw = (os.environ.get("FEDEX_UPLOAD_POLL_TIMEOUT_S") or "600").strip()
    try:
        return max(60.0, float(raw))
    except ValueError:
        return 600.0


def upload_poll_interval_s() -> float:
    raw = (os.environ.get("FEDEX_UPLOAD_POLL_INTERVAL_S") or "10").strip()
    try:
        return max(3.0, float(raw))
    except ValueError:
        return 10.0


def label_save_timeout_s() -> float:
    raw = (os.environ.get("FEDEX_LABEL_SAVE_TIMEOUT_S") or "120").strip()
    try:
        return max(30.0, float(raw))
    except ValueError:
        return 120.0


def pdf_page_wait_ms() -> int:
    raw = (os.environ.get("FEDEX_PDF_PAGE_WAIT_MS") or "8000").strip()
    try:
        return max(2000, int(raw))
    except ValueError:
        return 8000


def warehouse_print_pause_ms() -> int:
    """Pause on the label tab before opening Edge print (warehouse Zebra labels)."""
    raw = (os.environ.get("FEDEX_WAREHOUSE_PRINT_PAUSE_MS") or "4000").strip()
    try:
        return max(1500, int(raw))
    except ValueError:
        return 4000


def warehouse_after_print_ms() -> int:
    """Pause after each warehouse label print before closing the tab."""
    raw = (os.environ.get("FEDEX_WAREHOUSE_AFTER_PRINT_MS") or "6000").strip()
    try:
        return max(2000, int(raw))
    except ValueError:
        return 6000
