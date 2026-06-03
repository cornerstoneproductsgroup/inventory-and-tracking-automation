"""Paths and retailer labels for the morning pull-orders workflow."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path


def _p(env_key: str, default: str) -> Path:
    raw = (os.environ.get(env_key) or default).strip()
    return Path(raw)


@dataclass(frozen=True)
class RetailerPullPaths:
    key: str
    label: str
    pdf_dir: Path
    csv_dir: Path
    csv_merchant_id: str | None = None


_BASE = _p(
    "PULL_ORDERS_BASE",
    r"\\rygarcorp.com\shares\Cornerstone\Dot Com Packing Slips\1-Orders Before Extraction",
)
_CSV_ROOT = _p("PULL_ORDERS_CSV_ROOT", str(_BASE / "6-CSV Order Files"))
_DAILY_VENDOR = _p(
    "PULL_ORDERS_DAILY_VENDOR_DIR",
    str(_BASE / "Order Splitter Output" / "z- Daily Vendor Orders"),
)

RETAILERS: dict[str, RetailerPullPaths] = {
    "depot": RetailerPullPaths(
        key="depot",
        label="Depot",
        pdf_dir=_p("PULL_ORDERS_DEPOT_PDF_DIR", str(_BASE / "1-Depot")),
        csv_dir=_p("PULL_ORDERS_DEPOT_CSV_DIR", str(_CSV_ROOT / "Depot")),
        csv_merchant_id="thehomedepot",
    ),
    "lowes": RetailerPullPaths(
        key="lowes",
        label="Lowe's",
        pdf_dir=_p("PULL_ORDERS_LOWES_PDF_DIR", str(_BASE / "2-Lowe's")),
        csv_dir=_p("PULL_ORDERS_LOWES_CSV_DIR", str(_CSV_ROOT / "Lowe's")),
        csv_merchant_id="lowes",
    ),
    "thdso": RetailerPullPaths(
        key="thdso",
        label="Depot Special Order",
        pdf_dir=_p("PULL_ORDERS_THDSO_PDF_DIR", str(_BASE / "Depot Special Orders")),
        csv_dir=_p("PULL_ORDERS_THDSO_CSV_DIR", str(_CSV_ROOT / "Depot Special Order")),
        csv_merchant_id="thdso",
    ),
    "tractor": RetailerPullPaths(
        key="tractor",
        label="Tractor Supply",
        pdf_dir=_p("PULL_ORDERS_TRACTOR_PDF_DIR", str(_BASE / "3-Tractor Supply")),
        csv_dir=_p("PULL_ORDERS_TRACTOR_CSV_DIR", str(_CSV_ROOT / "Tractor Supply")),
        csv_merchant_id=None,
    ),
    "grainger": RetailerPullPaths(
        key="grainger",
        label="Grainger",
        pdf_dir=_p("PULL_ORDERS_GRAINGER_PDF_DIR", str(_BASE / "4-Grainger")),
        csv_dir=_p("PULL_ORDERS_GRAINGER_CSV_DIR", str(_CSV_ROOT / "Grainger")),
        csv_merchant_id=None,
    ),
}

COMMERCEHUB_HOME_URL = "https://dsm.commercehub.com/dsm/gotoHome.do"
COMMERCEHUB_PACKSLIPS_URL = "https://dsm.commercehub.com/dsm/gotoViewPackslips.do"
COMMERCEHUB_ORDER_FILES_URL = "https://dsm.commercehub.com/dsm/gotoViewOrders.do"

SPS_TRANSACTIONS_URL = "https://commerce.spscommerce.com/fulfillment/transactions/list/"


def date_stamp(d: date | None = None) -> str:
    """Format like 5-29-2026 (no leading zeros)."""
    d = d or date.today()
    return f"{d.month}-{d.day}-{d.year}"


def pdf_filename(label: str, d: date | None = None, page: int = 1) -> str:
    return f"{label} {date_stamp(d)} Page {page}.pdf"


def csv_filename(label: str, d: date | None = None) -> str:
    return f"{label} {date_stamp(d)}.csv"


def partner_text_to_key(partner_text: str) -> str | None:
    """Map retailer label (e.g. td.characterdata \"Lowe's\") to config key."""
    t = (partner_text or "").strip().lower()
    if not t:
        return None
    if "special" in t and ("home depot" in t or "thdso" in t):
        return "thdso"
    if t in ("lowe's", "lowes", "lowe s"):
        return "lowes"
    if "lowe" in t:
        return "lowes"
    if "home depot" in t:
        return "depot"
    return None


def merchant_column_to_key(merchant: str) -> str | None:
    m = (merchant or "").strip().lower()
    for key, cfg in RETAILERS.items():
        if cfg.csv_merchant_id and m == cfg.csv_merchant_id:
            return key
    return None
