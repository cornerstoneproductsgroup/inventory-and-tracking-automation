"""Paths and settings for Amazon Seller Central deferred-transaction download."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent

DEFAULT_AMAZON_BASE_DIR = r"\\rygarcorp.com\shares\Cornerstone\Invoice Reports\Amazon"
DEFAULT_INPUT_DIR = DEFAULT_AMAZON_BASE_DIR + r"\Input"

DEFAULT_HOME_URL = "https://sellercentral.amazon.com/home"
DEFAULT_REPORTS_URL = "https://sellercentral.amazon.com/payments/reports-repository"
DEFAULT_LOGIN_URL = "https://sellercentral.amazon.com/ap/signin"

STORAGE_STATE = Path(
    (os.environ.get("AMAZON_SELLER_STORAGE_STATE") or str(_SCRIPT_DIR / "amazon_seller_storage_state.json")).strip()
)


def resolve_input_dir() -> Path:
    raw = (os.environ.get("AMAZON_INVOICE_INPUT_DIR") or DEFAULT_INPUT_DIR).strip()
    return Path(raw).expanduser()


def use_system_chrome_profile() -> bool:
    """Use installed Chrome User Data (Default profile) via CDP — same login as daily Chrome."""
    raw = (os.environ.get("AMAZON_CHROME_USE_SYSTEM_PROFILE") or "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def amazon_browser_cdp_port() -> int:
    # 9348 avoids Edge/other tools that often bind 9222
    raw = (
        os.environ.get("AMAZON_CHROME_CDP_PORT")
        or os.environ.get("AMAZON_BROWSER_CDP_PORT")
        or "9348"
    ).strip()
    try:
        return max(1024, int(raw))
    except ValueError:
        return 9222


def chrome_user_data_dir() -> Path | None:
    """
    Optional isolated Playwright profile (only when AMAZON_CHROME_USE_SYSTEM_PROFILE=false).

    Default: None — automation uses your installed Chrome profile via CDP instead.
  """
    raw = (os.environ.get("AMAZON_CHROME_USER_DATA_DIR") or "").strip()
    if raw.lower() in ("0", "false", "no", "off", "none", "disable", "disabled"):
        return None
    if raw:
        return Path(raw).expanduser()
    if use_system_chrome_profile():
        return None
    return _SCRIPT_DIR / ".amazon-chrome-profile"


def chrome_channel() -> str:
    return (os.environ.get("AMAZON_CHROME_CHANNEL") or "chrome").strip() or "chrome"


def chrome_cdp_url() -> str:
    """Attach to Chrome already running with --remote-debugging-port=9222."""
    return (os.environ.get("AMAZON_CHROME_CDP_URL") or "").strip()


def uses_chrome_session() -> bool:
    return bool(chrome_cdp_url() or use_system_chrome_profile() or chrome_user_data_dir())


def amazon_input_filename(report_day: date) -> str:
    """e.g. Amazon Invoice 6-24-2026.csv"""
    return f"Amazon Invoice {report_day.month}-{report_day.day}-{report_day.year}.csv"


def amazon_input_path(run_day: date | None = None) -> Path:
    d = run_day or date.today()
    return resolve_input_dir() / amazon_input_filename(d)


def report_ready_poll_interval_s() -> float:
    raw = (os.environ.get("AMAZON_REPORT_POLL_INTERVAL_S") or "8").strip()
    try:
        return max(3.0, float(raw))
    except ValueError:
        return 8.0


def request_report_settle_s() -> float:
    """Wait after Request Report before first Refresh (Amazon ~10s)."""
    raw = (os.environ.get("AMAZON_REQUEST_REPORT_SETTLE_S") or "10").strip()
    try:
        return max(3.0, float(raw))
    except ValueError:
        return 10.0


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
    v = (os.environ.get("AMAZON_DOWNLOAD_AUTO_POSTPROCESS") or "false").strip().lower()
    return v in ("1", "true", "yes", "on")


def headless() -> bool:
    v = (os.environ.get("AMAZON_SELLER_HEADLESS") or "false").strip().lower()
    return v in ("1", "true", "yes", "on")


def seller_download_enabled() -> bool:
    v = (os.environ.get("AMAZON_SELLER_DOWNLOAD_ENABLED") or "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    return True


ON_HOLD_REASON = (
    "Amazon Seller Central download needs either Chrome session reuse or explicit enable.\n"
    "  Set AMAZON_CHROME_USER_DATA_DIR (or AMAZON_CHROME_CDP_URL) to reuse Chrome login, or\n"
    "  set AMAZON_SELLER_DOWNLOAD_ENABLED=true with AMAZON_SELLER_EMAIL/PASSWORD in .env."
)
