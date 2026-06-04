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
    "thdso": _p(
        "WORLDSHIP_LABELS_THDSO_DIR",
        str(_PACKING_SLIPS / "12-Depot Special Orders" / "1 - UPS Shipping Labels"),
    ),
    "tractor": _p(
        "WORLDSHIP_LABELS_TRACTOR_DIR",
        str(_PACKING_SLIPS / "6-Tractor Supply" / "1 - UPS Shipping Labels"),
    ),
}

COL_SKU = (os.environ.get("WORLDSHIP_COL_SKU") or "L").strip().upper()
COL_PO = (os.environ.get("WORLDSHIP_COL_PO") or "O").strip().upper()
COL_RETAILER = (os.environ.get("WORLDSHIP_COL_RETAILER") or "U").strip().upper()
DATA_START_ROW = int((os.environ.get("WORLDSHIP_DATA_START_ROW") or "2").strip() or "2")

# SKU → vendor folder maps (copies of Order Splitter vendor_map_*.xlsx on the Cornerstone share).
VENDOR_MAP_DIR = _p(
    "WORLDSHIP_VENDOR_MAP_DIR",
    r"\\rygarcorp.com\shares\Cornerstone\Automation\Vendor Maps for SKUs",
)

RETAILER_VENDOR_MAP_FILES: dict[str, str] = {
    "depot": "vendor_map_hd.xlsx",
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
    raw = (os.environ.get("WORLDSHIP_SAVE_BETWEEN_LABELS_S") or "4").strip()
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 4.0


def retailer_merchant_to_key(merchant: str) -> str:
    m = (merchant or "").strip().lower()
    if not m:
        raise ValueError("Retailer/merchant column is empty.")
    if "thdso" in m or ("special" in m and ("depot" in m or "home depot" in m)):
        return "thdso"
    if "tractor" in m:
        return "tractor"
    if "homedepot" in m.replace(" ", "") or "home depot" in m or m == "depot":
        return "depot"
    raise ValueError(f"Unsupported retailer/merchant value: {merchant!r}")
