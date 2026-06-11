"""Paths and settings for UPS.com batch file shipping (Depot, Special Order, Tractor)."""

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

_CSV_OUTPUT_ROOT = (
    _PACKING_SLIPS
    / "1-Orders Before Extraction"
    / "Order Splitter Output"
    / "CSV File Output"
)

DEPOT_CSV_OUTPUT_DIR = _p(
    "UPS_DEPOT_CSV_DIR",
    str(_CSV_OUTPUT_ROOT / "Depot"),
)

THDSO_CSV_OUTPUT_DIR = _p(
    "UPS_THDSO_CSV_DIR",
    str(_CSV_OUTPUT_ROOT / "Depot Special Order"),
)

TRACTOR_CSV_OUTPUT_DIR = _p(
    "UPS_TRACTOR_CSV_DIR",
    str(_CSV_OUTPUT_ROOT / "Tractor Supply"),
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

THDSO_LABELS_MAIN_DIR = _p(
    "UPS_THDSO_LABELS_MAIN_DIR",
    str(
        _PACKING_SLIPS
        / "12-Depot Special Orders"
        / "1 - UPS Shipping Labels"
        / "1 - Main File For The Day"
    ),
)

TRACTOR_LABELS_MAIN_DIR = _p(
    "UPS_TRACTOR_LABELS_MAIN_DIR",
    str(
        _PACKING_SLIPS
        / "6-Tractor Supply"
        / "1 - UPS Shipping Labels"
        / "1 - Main File For The Day"
    ),
)

UPS_BATCH_LANE_ORDER: tuple[str, ...] = ("depot", "thdso", "tractor")

_LANE_CSV_DIRS: dict[str, Path] = {
    "depot": DEPOT_CSV_OUTPUT_DIR,
    "thdso": THDSO_CSV_OUTPUT_DIR,
    "tractor": TRACTOR_CSV_OUTPUT_DIR,
}

_LANE_LABELS_DIRS: dict[str, Path] = {
    "depot": DEPOT_LABELS_MAIN_DIR,
    "thdso": THDSO_LABELS_MAIN_DIR,
    "tractor": TRACTOR_LABELS_MAIN_DIR,
}

_LANE_FILE_LABELS: dict[str, str] = {
    "depot": "Depot",
    "thdso": "Depot Special Order",
    "tractor": "Tractor Supply",
}

_LANE_LABELS_PDF_PREFIX: dict[str, str] = {
    "depot": "Home Depot",
    "thdso": "Depot Special Order",
    "tractor": "Tractor Supply",
}

_LANE_CSV_PATH_ENV: dict[str, str] = {
    "depot": "UPS_DEPOT_CSV_PATH",
    "thdso": "UPS_THDSO_CSV_PATH",
    "tractor": "UPS_TRACTOR_CSV_PATH",
}

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


def ups_browser_channel(browser_cfg: dict | None = None) -> str:
    """Installed browser for UPS: msedge or chrome."""
    browser_cfg = browser_cfg or {}
    raw = (
        (os.environ.get("UPS_BROWSER_CHANNEL") or "").strip().lower()
        or str(browser_cfg.get("channel") or "").strip().lower()
        or "chrome"
    )
    if raw in ("edge", "msedge", "microsoft-edge"):
        return "msedge"
    return "chrome"


def browser_display_name(channel: str | None = None) -> str:
    return "Edge" if (channel or ups_browser_channel()) == "msedge" else "Chrome"


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


def system_edge_user_data_dir() -> Path | None:
    """Installed Microsoft Edge profile root."""
    override = (os.environ.get("UPS_EDGE_USER_DATA_DIR") or "").strip()
    if override:
        path = Path(override)
        return path if path.is_dir() else None
    local = (os.environ.get("LOCALAPPDATA") or "").strip()
    if not local:
        return None
    path = Path(local) / "Microsoft" / "Edge" / "User Data"
    return path if path.is_dir() else None


def system_browser_user_data_dir(browser_cfg: dict | None = None) -> Path | None:
    if ups_browser_channel(browser_cfg) == "msedge":
        return system_edge_user_data_dir()
    return system_chrome_user_data_dir()


def chrome_profile_directory() -> str:
    """Profile folder name under User Data (usually Default or Profile 1)."""
    return (
        (os.environ.get("UPS_BROWSER_PROFILE") or "").strip()
        or (os.environ.get("UPS_CHROME_PROFILE") or "").strip()
        or (os.environ.get("UPS_EDGE_PROFILE") or "").strip()
        or "Default"
    )


def browser_profile_directory() -> str:
    return chrome_profile_directory()


def chrome_cdp_port() -> int:
    return browser_cdp_port()


def browser_cdp_port(browser_cfg: dict | None = None) -> int:
    raw = (
        (os.environ.get("UPS_BROWSER_CDP_PORT") or "").strip()
        or (os.environ.get("UPS_CHROME_CDP_PORT") or "").strip()
        or ("9345" if ups_browser_channel(browser_cfg) == "msedge" else "9344")
    )
    try:
        return max(1024, int(raw))
    except ValueError:
        return 9345 if ups_browser_channel(browser_cfg) == "msedge" else 9344


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


def edge_executable() -> Path | None:
    override = (os.environ.get("UPS_EDGE_EXE") or "").strip()
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
        cand = Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        if cand.is_file():
            return cand
    return None


def browser_executable(browser_cfg: dict | None = None) -> Path | None:
    if ups_browser_channel(browser_cfg) == "msedge":
        return edge_executable()
    return chrome_executable()


def browser_process_image(browser_cfg: dict | None = None) -> str:
    return "msedge.exe" if ups_browser_channel(browser_cfg) == "msedge" else "chrome.exe"


def allow_unsafe_cdp() -> bool:
    """
    CDP against a real browser profile matches infostealer behavior (Huntress flags it).
    Only enable when IT has explicitly approved: UPS_ALLOW_UNSAFE_CDP=1
    """
    return _env_bool("UPS_ALLOW_UNSAFE_CDP", default=False)


def use_chrome_cdp_launch(browser_cfg: dict | None = None) -> bool:
    """CDP is off unless UPS_BROWSER_MODE=cdp|manual AND UPS_ALLOW_UNSAFE_CDP=1."""
    mode = ups_browser_mode(browser_cfg)
    return mode in ("cdp", "manual") and allow_unsafe_cdp()


def chrome_cdp_env_disabled() -> bool:
    return not allow_unsafe_cdp()


def ups_browser_mode(browser_cfg: dict | None = None) -> str:
    """
    How to open UPS in a browser.

    - dedicated (default): isolated ups_browser_profile — safe for Huntress/IT
    - cdp: attach to real Chrome/Edge profile (requires UPS_ALLOW_UNSAFE_CDP=1)
    - manual: connect to debug browser you start (requires UPS_ALLOW_UNSAFE_CDP=1)
    """
    browser_cfg = browser_cfg or {}
    raw = (
        (os.environ.get("UPS_BROWSER_MODE") or "").strip().lower()
        or str(browser_cfg.get("browser_mode") or "").strip().lower()
        or "dedicated"
    )
    if raw in ("dedicated", "profile", "local"):
        return "dedicated"
    if raw in ("manual", "attach"):
        return "manual"
    if raw in ("cdp", "system", "chrome"):
        return "cdp"
    return "dedicated"


def dedicated_ups_profile_dir(browser_cfg: dict | None = None) -> Path:
    if ups_browser_channel(browser_cfg) == "msedge":
        return _INVENTORY_ROOT / "ups_edge_browser_profile"
    return DEFAULT_BROWSER_PROFILE_DIR


def kill_chrome_before_launch() -> bool:
    return _env_bool("UPS_KILL_CHROME", default=True)


def use_system_chrome_profile(browser_cfg: dict | None = None) -> bool:
    """Only when explicitly using unsafe CDP against the installed browser profile."""
    browser_cfg = browser_cfg or {}
    if ups_browser_mode(browser_cfg) != "cdp" or not allow_unsafe_cdp():
        return False
    env_raw = (os.environ.get("UPS_USE_SYSTEM_CHROME") or "").strip()
    if env_raw:
        return _env_bool("UPS_USE_SYSTEM_CHROME", default=True)
    if browser_cfg.get("use_system_chrome") is False:
        return False
    return True


def resolve_browser_user_data_dir(browser_cfg: dict | None = None) -> Path | None:
    """
    Profile directory for launch_persistent_context.

    Default: isolated ups_browser_profile (never the user's real Chrome/Edge folder).
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
        return path if path.is_absolute() else dedicated_ups_profile_dir(browser_cfg).parent / path

    if use_system_chrome_profile(browser_cfg):
        system_data = system_browser_user_data_dir(browser_cfg)
        if system_data is not None:
            return system_data

    raw = str(browser_cfg.get("user_data_dir") or "").strip()
    if raw and raw.lower() not in ("0", "false", "no", "off"):
        path = Path(raw)
        return path if path.is_absolute() else dedicated_ups_profile_dir(browser_cfg).parent / path

    return dedicated_ups_profile_dir(browser_cfg)


def normalize_ups_lane(lane: str | None) -> str:
    raw = (lane or "depot").strip().lower()
    if raw in ("thdso", "depot_special", "depot_special_order", "special_order"):
        return "thdso"
    if raw in ("tractor", "tsc", "tractor_supply"):
        return "tractor"
    if raw in ("depot", "home_depot", "homedepot"):
        return "depot"
    raise ValueError(f"Unknown UPS batch lane: {lane!r} (use depot, thdso, or tractor)")


def lane_file_label(lane: str) -> str:
    return _LANE_FILE_LABELS[normalize_ups_lane(lane)]


def lane_csv_dir(lane: str) -> Path:
    return _LANE_CSV_DIRS[normalize_ups_lane(lane)]


def lane_labels_dir(lane: str) -> Path:
    return _LANE_LABELS_DIRS[normalize_ups_lane(lane)]


def lane_csv_path_env_key(lane: str) -> str:
    return _LANE_CSV_PATH_ENV[normalize_ups_lane(lane)]


def lane_output_basename(lane: str, order_date: date | None = None) -> str:
    """e.g. Depot 6-10-2026 Output.csv"""
    d = order_date or date.today()
    return f"{lane_file_label(lane)} {date_stamp(d)} Output.csv"


def lane_output_path(lane: str, order_date: date | None = None) -> Path:
    return lane_csv_dir(lane) / lane_output_basename(lane, order_date)


def lane_labels_pdf_name(lane: str, order_date: date | None = None) -> str:
    """e.g. Home Depot 6-10-2026 Labels.pdf"""
    d = order_date or date.today()
    prefix = _LANE_LABELS_PDF_PREFIX[normalize_ups_lane(lane)]
    return f"{prefix} {date_stamp(d)} Labels.pdf"


def lane_labels_pdf_path(lane: str, order_date: date | None = None) -> Path:
    return lane_labels_dir(lane) / lane_labels_pdf_name(lane, order_date)


def depot_output_basename(order_date: date | None = None) -> str:
    return lane_output_basename("depot", order_date)


def depot_output_path(order_date: date | None = None) -> Path:
    return lane_output_path("depot", order_date)


def depot_labels_pdf_name(order_date: date | None = None) -> str:
    return lane_labels_pdf_name("depot", order_date)


def depot_labels_pdf_path(order_date: date | None = None) -> Path:
    return lane_labels_pdf_path("depot", order_date)


def label_save_timeout_s() -> float:
    raw = (os.environ.get("UPS_LABEL_SAVE_TIMEOUT_S") or "180").strip()
    try:
        return max(30.0, float(raw))
    except ValueError:
        return 180.0
