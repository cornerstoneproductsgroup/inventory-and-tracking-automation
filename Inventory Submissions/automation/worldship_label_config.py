"""Paths and column settings for WorldShip label save automation."""

from __future__ import annotations

import os
from pathlib import Path


def _p(env_key: str, default: str) -> Path:
    raw = (os.environ.get(env_key) or default).strip()
    return Path(raw)


_PACKING_SLIPS = _p(
    "WORLDSHIP_PACKING_SLIPS_BASE",
    r"\\rygarcorp.com\shares\Cornerstone\Dot Com Packing Slips",
)

CORNERSTONE_MASTER_DIR = _p(
    "WORLDSHIP_CORNERSTONE_MASTER_DIR",
    r"\\svr01\Cornerstone\Dot Com Packing Slips\zzz - Worldship Shipment Files\Cornerstone",
)

# Fallback when svr01 path is unavailable on a PC.
CORNERSTONE_MASTER_DIR_FALLBACK = _PACKING_SLIPS / "zzz - Worldship Shipment Files" / "Cornerstone"

LABEL_ROOTS: dict[str, Path] = {
    "depot": _p(
        "WORLDSHIP_LABELS_DEPOT_DIR",
        str(_PACKING_SLIPS / "2-Home Depot" / "1 - UPS Shipping Labels"),
    ),
    "dfc": _p(
        "WORLDSHIP_LABELS_DFC_DIR",
        str(_PACKING_SLIPS / "7-DFC" / "1 - Shipping Labels"),
    ),
    "thdso": _p(
        "WORLDSHIP_LABELS_THDSO_DIR",
        str(_PACKING_SLIPS / "12-Depot Special Orders" / "1 - UPS Shipping Labels"),
    ),
    "tractor": _p(
        "WORLDSHIP_LABELS_TRACTOR_DIR",
        str(_PACKING_SLIPS / "6-Tractor Supply" / "1 - UPS Shipping Labels"),
    ),
}

# Legacy DFC path (PDFs saved directly under 7-DFC\<Vendor> before routing fix).
DFC_LEGACY_VENDOR_LABEL_ROOT = _p(
    "WORLDSHIP_DFC_LEGACY_VENDOR_DIR",
    str(_PACKING_SLIPS / "7-DFC"),
)

DAILY_VENDOR_ORDERS_DIR = _p(
    "WORLDSHIP_DAILY_VENDOR_DIR",
    str(
        _PACKING_SLIPS
        / "1-Orders Before Extraction"
        / "Order Splitter Output"
        / "z- Daily Vendor Orders"
    ),
)

RETAILER_DAILY_LABEL_PREFIX: dict[str, str] = {
    "depot": (os.environ.get("WORLDSHIP_DEPOT_DAILY_LABEL_PREFIX") or "Home Depot").strip(),
    "dfc": (os.environ.get("WORLDSHIP_DFC_DAILY_LABEL_PREFIX") or "Home Depot DFC").strip(),
    "thdso": (
        os.environ.get("WORLDSHIP_THDSO_DAILY_LABEL_PREFIX") or "Depot Special Order"
    ).strip(),
    "tractor": (
        os.environ.get("WORLDSHIP_TRACTOR_DAILY_LABEL_PREFIX") or "Tractor Supply"
    ).strip(),
}

COL_SKU = (os.environ.get("WORLDSHIP_COL_SKU") or "L").strip().upper()
COL_PO = (os.environ.get("WORLDSHIP_COL_PO") or "O").strip().upper()
COL_RETAILER = (os.environ.get("WORLDSHIP_COL_RETAILER") or "U").strip().upper()
# LabelPDF = save to share; Label1 = warehouse print (WorldShip direct to printer).
COL_LABEL_PR = (os.environ.get("WORLDSHIP_COL_LABEL_PR") or "X").strip().upper()
DATA_START_ROW = int((os.environ.get("WORLDSHIP_DATA_START_ROW") or "2").strip() or "2")

# SKU → vendor folder maps (copies of Order Splitter vendor_map_*.xlsx on the Cornerstone share).
VENDOR_MAP_DIR = _p(
    "WORLDSHIP_VENDOR_MAP_DIR",
    r"\\rygarcorp.com\shares\Cornerstone\Automation\Vendor Maps for SKUs",
)

RETAILER_VENDOR_MAP_FILES: dict[str, str] = {
    "depot": "vendor_map_hd.xlsx",
    "dfc": "vendor_map_hd.xlsx",
    "thdso": "vendor_map_hd.xlsx",
    "tractor": "vendor_map_tsc.xlsx",
    "lowes": "vendor_map_lowes.xlsx",
}

_ORDER_SPLITTER_BASE = _p(
    "WORLDSHIP_ORDER_SPLITTER_DIR",
    str(_PACKING_SLIPS / "1-Orders Before Extraction" / "Order Splitter Output"),
)

_DEFAULT_VENDOR_MAP_ENV = (os.environ.get("WORLDSHIP_VENDOR_MAP") or "").strip()

DEFAULT_VENDOR_MAP_CANDIDATES: tuple[Path, ...] = tuple(
    p
    for p in (
        Path(_DEFAULT_VENDOR_MAP_ENV) if _DEFAULT_VENDOR_MAP_ENV else None,
        _ORDER_SPLITTER_BASE / "SKU Vendor Map.csv",
        _ORDER_SPLITTER_BASE / "Vendor SKU Map.csv",
        _ORDER_SPLITTER_BASE / "SKU to Vendor.csv",
        _ORDER_SPLITTER_BASE / "Vendor Map.csv",
        _ORDER_SPLITTER_BASE / "SKU Vendor Map.xlsx",
        _ORDER_SPLITTER_BASE / "Vendor SKU Map.xlsx",
    )
    if p is not None
)


def label_extension() -> str:
    raw = (os.environ.get("WORLDSHIP_LABEL_EXT") or ".pdf").strip()
    if not raw:
        return ""
    return raw if raw.startswith(".") else f".{raw}"


def processing_timeout_s(*, order_count: int | None = None) -> float:
    """Max wait for Automatic Processing Progress before first save dialog."""
    base_raw = (os.environ.get("WORLDSHIP_PROCESSING_TIMEOUT_S") or "600").strip()
    per_raw = (os.environ.get("WORLDSHIP_PROCESSING_PER_ORDER_S") or "45").strip()
    try:
        base = max(60.0, float(base_raw))
    except ValueError:
        base = 600.0
    try:
        per = max(10.0, float(per_raw))
    except ValueError:
        per = 45.0
    if order_count and order_count > 0:
        return max(base, order_count * per)
    return base


def save_dialog_timeout_s(*, first: bool) -> float:
    key = "WORLDSHIP_FIRST_SAVE_TIMEOUT_S" if first else "WORLDSHIP_SAVE_TIMEOUT_S"
    default = "180" if first else "90"
    raw = (os.environ.get(key) or default).strip()
    try:
        return max(15.0, float(raw))
    except ValueError:
        return 180.0 if first else 90.0


def label_save_gap_s() -> float:
    """Pause after each label save before waiting for the next Save dialog."""
    raw = (os.environ.get("WORLDSHIP_SAVE_BETWEEN_LABELS_S") or "2").strip()
    try:
        return max(0.5, float(raw))
    except ValueError:
        return 2.0


def warehouse_print_wait_s() -> float:
    """How long to wait for a Save dialog on warehouse-print rows (usually none)."""
    raw = (os.environ.get("WORLDSHIP_PRINT_WAIT_S") or "15").strip()
    try:
        return max(3.0, float(raw))
    except ValueError:
        return 15.0


def retailer_merchant_to_key(merchant: str) -> str:
    m = (merchant or "").strip().lower()
    if not m:
        raise ValueError("Retailer/merchant column is empty.")
    if "thdso" in m or ("special" in m and ("depot" in m or "home depot" in m)):
        return "thdso"
    if "dfc" in m:
        return "dfc"
    if "tractor" in m:
        return "tractor"
    if "homedepot" in m.replace(" ", "") or "home depot" in m or m == "depot":
        return "depot"
    raise ValueError(f"Unsupported retailer/merchant value: {merchant!r}")


def date_stamp(d=None) -> str:
    from datetime import date

    x = d or date.today()
    return f"{x.month}-{x.day}-{x.year}"


def daily_vendor_label_pdf_path(
    retailer_key: str,
    vendor_folder: str,
    *,
    order_date=None,
) -> Path:
    from datetime import date

    prefix = RETAILER_DAILY_LABEL_PREFIX.get(retailer_key, retailer_key)
    stamp = date_stamp(order_date or date.today())
    return DAILY_VENDOR_ORDERS_DIR / vendor_folder / f"{prefix} {stamp} Labels.pdf"


def label_postprocess_retailer_keys() -> frozenset[str]:
    raw = (os.environ.get("WORLDSHIP_LABEL_POSTPROCESS_RETAILERS") or "dfc").strip()
    if not raw:
        return frozenset()
    return frozenset(k.strip().lower() for k in raw.split(",") if k.strip())


def label_width_pts() -> float:
    raw = (os.environ.get("WORLDSHIP_LABEL_WIDTH_IN") or "4").strip()
    try:
        return max(1.0, float(raw)) * 72.0
    except ValueError:
        return 4.0 * 72.0


def label_height_pts() -> float:
    raw = (os.environ.get("WORLDSHIP_LABEL_HEIGHT_IN") or "6").strip()
    try:
        return max(1.0, float(raw)) * 72.0
    except ValueError:
        return 6.0 * 72.0


def label_crop_x_pts() -> float:
    raw = (os.environ.get("WORLDSHIP_LABEL_CROP_X_IN") or "0").strip()
    try:
        return max(0.0, float(raw)) * 72.0
    except ValueError:
        return 0.0


def label_crop_y_from_top_pts() -> float:
    raw = (os.environ.get("WORLDSHIP_LABEL_CROP_Y_FROM_TOP_IN") or "0").strip()
    try:
        return max(0.0, float(raw)) * 72.0
    except ValueError:
        return 0.0
