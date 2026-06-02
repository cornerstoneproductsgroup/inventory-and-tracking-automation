"""Paths and settings for Amazon Seller Central deferred-transaction download."""

from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent

DEFAULT_AMAZON_BASE_DIR = r"\\rygarcorp.com\shares\Cornerstone\Invoice Reports\Amazon"
DEFAULT_INPUT_DIR = DEFAULT_AMAZON_BASE_DIR + r"\Input"

DEFAULT_REPORTS_URL = (
    "https://sellercentral.amazon.com/payments/reports-repository"
)
DEFAULT_LOGIN_URL = "https://sellercentral.amazon.com/ap/signin"

STORAGE_STATE = Path(
    (os.environ.get("AMAZON_SELLER_STORAGE_STATE") or str(_SCRIPT_DIR / "amazon_seller_storage_state.json")).strip()
)


def resolve_input_dir() -> Path:
    raw = (os.environ.get("AMAZON_INVOICE_INPUT_DIR") or DEFAULT_INPUT_DIR).strip()
    return Path(raw).expanduser()


def report_day_for_run(run_day: date | None = None) -> date:
    """Previous calendar day (file name date when run today)."""
    d = run_day or date.today()
    return d - timedelta(days=1)


def amazon_input_filename(report_day: date) -> str:
    """e.g. Amazon Invoice Report 5-31-2026 Input.csv"""
    return (
        f"Amazon Invoice Report {report_day.month}-{report_day.day}-{report_day.year} Input.csv"
    )


def amazon_input_path(run_day: date | None = None) -> Path:
    return resolve_input_dir() / amazon_input_filename(report_day_for_run(run_day))


def report_ready_poll_interval_s() -> float:
    raw = (os.environ.get("AMAZON_REPORT_POLL_INTERVAL_S") or "8").strip()
    try:
        return max(3.0, float(raw))
    except ValueError:
        return 8.0


def report_ready_max_attempts() -> int:
    raw = (os.environ.get("AMAZON_REPORT_MAX_REFRESH") or "20").strip()
    try:
        return max(3, int(raw))
    except ValueError:
        return 20


def download_timeout_ms() -> int:
    raw = (os.environ.get("AMAZON_DOWNLOAD_TIMEOUT_MS") or "120000").strip()
    try:
        return max(30_000, int(raw))
    except ValueError:
        return 120_000


def auto_postprocess_after_download() -> bool:
    v = (os.environ.get("AMAZON_DOWNLOAD_AUTO_POSTPROCESS") or "true").strip().lower()
    return v not in ("0", "false", "no", "")


def headless() -> bool:
    v = (os.environ.get("AMAZON_SELLER_HEADLESS") or "false").strip().lower()
    return v in ("1", "true", "yes", "on")


def seller_download_enabled() -> bool:
    """
    Seller Central browser download is ON HOLD (2FA every run).
    Set AMAZON_SELLER_DOWNLOAD_ENABLED=true in .env to re-enable when ready.
    """
    v = (os.environ.get("AMAZON_SELLER_DOWNLOAD_ENABLED") or "false").strip().lower()
    return v in ("1", "true", "yes", "on")


ON_HOLD_REASON = (
    "Amazon Seller Central download is on hold: sign-in requires phone 2FA on every "
    "automated browser session. Code is kept in invoice report/ for a future approach "
    "(manual OTP, persistent profile, etc.). Set AMAZON_SELLER_DOWNLOAD_ENABLED=true to "
    "re-enable after that is solved."
)
