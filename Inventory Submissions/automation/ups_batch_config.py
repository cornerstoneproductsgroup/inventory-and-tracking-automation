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


def _env_bool(name: str, *, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw in ("0", "false", "no", "off"):
        return False
    return raw in ("1", "true", "yes", "on")


def system_chrome_user_data_dir() -> Path | None:
    """Installed Google Chrome profile root (cookies / UPS login live here)."""
    override = (os.environ.get("UPS_CHROME_USER_DATA_DIR") or "").strip()
    if override:
        path = Path(override)
        return path if path.is_dir() else None
    local = (os.environ.get("LOCALAPPDATA") or "").strip()
    if not local:
        return None
    path = Path(local) / "Google" / "Chrome" / "User Data"
    return path if path.is_dir() else None


def chrome_profile_directory() -> str:
    """Chrome profile folder name under User Data (usually Default or Profile 1)."""
    return (os.environ.get("UPS_CHROME_PROFILE") or "Default").strip() or "Default"


def chrome_cdp_port() -> int:
    raw = (os.environ.get("UPS_CHROME_CDP_PORT") or "9344").strip()
    try:
        return max(1024, int(raw))
    except ValueError:
        return 9344


def chrome_executable() -> Path | None:
    override = (os.environ.get("UPS_CHROME_EXE") or "").strip()
    if override:
        path = Path(override)
        return path if path.is_file() else None
    roots = [
        os.environ.get("PROGRAMFILES", ""),
        os.environ.get("PROGRAMFILES(X86)", ""),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    for root in roots:
        if not root:
            continue
        cand = Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
        if cand.is_file():
            return cand
    return None


def use_chrome_cdp_launch(browser_cfg: dict | None = None) -> bool:
    """Optional CDP launch — off by default; Playwright Chrome is more reliable on RDP."""
    browser_cfg = browser_cfg or {}
    env_raw = (os.environ.get("UPS_USE_CHROME_CDP") or "").strip()
    if env_raw:
        return _env_bool("UPS_USE_CHROME_CDP", default=False)
    if browser_cfg.get("use_chrome_cdp") is True:
        return True
    return False


def kill_chrome_before_launch() -> bool:
    return _env_bool("UPS_KILL_CHROME", default=True)


def use_system_chrome_profile(browser_cfg: dict | None = None) -> bool:
    browser_cfg = browser_cfg or {}
    env_raw = (os.environ.get("UPS_USE_SYSTEM_CHROME") or "").strip()
    if env_raw:
        return _env_bool("UPS_USE_SYSTEM_CHROME", default=True)
    if browser_cfg.get("use_system_chrome") is False:
        return False
    return True


def resolve_browser_user_data_dir(browser_cfg: dict | None = None) -> Path | None:
    """
    Profile directory for launch_persistent_context.

    Priority: explicit UPS_USER_DATA_DIR → system Chrome (default) →
    ups_batch.json user_data_dir → ups_browser_profile.
    """
    browser_cfg = browser_cfg or {}
    env_persistent = (os.environ.get("UPS_USE_PERSISTENT_PROFILE") or "").strip()
    if env_persistent and not _env_bool("UPS_USE_PERSISTENT_PROFILE", default=True):
        return None
    if browser_cfg.get("use_persistent_profile") is False:
        return None

    explicit = (os.environ.get("UPS_USER_DATA_DIR") or "").strip()
    if explicit and explicit.lower() not in ("0", "false", "no", "off"):
        path = Path(explicit)
        return path if path.is_absolute() else DEFAULT_BROWSER_PROFILE_DIR.parent / path

    if use_system_chrome_profile(browser_cfg):
        chrome_data = system_chrome_user_data_dir()
        if chrome_data is not None:
            return chrome_data

    raw = str(browser_cfg.get("user_data_dir") or "").strip()
    if raw and raw.lower() not in ("0", "false", "no", "off"):
        path = Path(raw)
        return path if path.is_absolute() else DEFAULT_BROWSER_PROFILE_DIR.parent / path

    return DEFAULT_BROWSER_PROFILE_DIR


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
