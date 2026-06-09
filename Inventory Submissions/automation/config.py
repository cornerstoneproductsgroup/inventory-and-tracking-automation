import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_INVENTORY_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _INVENTORY_ROOT / ".env"


def _load_env() -> None:
    """Load Inventory Submissions/.env regardless of process cwd."""
    load_dotenv(_ENV_FILE, override=False)


@dataclass
class Settings:
    rithum_url: str
    rithum_username: str
    rithum_password: str
    sps_url: str
    sps_username: str
    sps_password: str
    headless: bool
    timeout_ms: int


@dataclass
class SpsSettings:
    sps_url: str
    sps_username: str
    sps_password: str
    headless: bool
    timeout_ms: int


def _to_bool(value: str, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_sps_settings() -> SpsSettings:
    """SPS-only settings (does not require Rithum credentials)."""
    _load_env()

    sps_url = os.getenv("SPS_URL", "https://commerce.spscommerce.com")
    sps_username = os.getenv("SPS_USERNAME", "")
    sps_password = os.getenv("SPS_PASSWORD", "")
    headless = _to_bool(os.getenv("HEADLESS", "false"), default=False)
    timeout_ms = int(os.getenv("TIMEOUT_MS", "30000"))

    if not sps_username:
        raise ValueError(f"Missing SPS_USERNAME in {_ENV_FILE} (or environment).")
    if not sps_password:
        raise ValueError(f"Missing SPS_PASSWORD in {_ENV_FILE} (or environment).")

    return SpsSettings(
        sps_url=sps_url,
        sps_username=sps_username,
        sps_password=sps_password,
        headless=headless,
        timeout_ms=timeout_ms,
    )


def apply_rithum_env_to_lowes_config(config: dict) -> dict:
    """
    Overlay CommerceHub login from Inventory Submissions/.env onto a Lowe's config dict.

    Same RITHUM_USERNAME / RITHUM_PASSWORD / RITHUM_URL used by commercehub_chain,
    inventory, and tracking — not credentials embedded in config.json.
    """
    _load_env()
    username = (os.getenv("RITHUM_USERNAME") or "").strip()
    password = (os.getenv("RITHUM_PASSWORD") or "").strip()
    login_url = (os.getenv("RITHUM_URL") or "").strip()
    if not username:
        raise ValueError(f"Missing RITHUM_USERNAME in {_ENV_FILE} (or environment).")
    if not password:
        raise ValueError(f"Missing RITHUM_PASSWORD in {_ENV_FILE} (or environment).")
    rithum = config.setdefault("rithum", {})
    rithum["username"] = username
    rithum["password"] = password
    if login_url:
        rithum["login_url"] = login_url
    return config


def load_settings() -> Settings:
    _load_env()

    rithum_url = os.getenv("RITHUM_URL", "https://dsm.commercehub.com/dsm/gotoHome.do")
    rithum_username = os.getenv("RITHUM_USERNAME", "")
    rithum_password = os.getenv("RITHUM_PASSWORD", "")
    sps_url = os.getenv("SPS_URL", "https://commerce.spscommerce.com")
    sps_username = os.getenv("SPS_USERNAME", "")
    sps_password = os.getenv("SPS_PASSWORD", "")
    # Default visible browser for desktop runs; set HEADLESS=true for servers/CI.
    headless = _to_bool(os.getenv("HEADLESS", "false"), default=False)
    timeout_ms = int(os.getenv("TIMEOUT_MS", "30000"))

    if not rithum_username:
        raise ValueError(f"Missing RITHUM_USERNAME in {_ENV_FILE} (or environment).")
    if not rithum_password:
        raise ValueError(f"Missing RITHUM_PASSWORD in {_ENV_FILE} (or environment).")
    if not sps_username:
        raise ValueError(f"Missing SPS_USERNAME in {_ENV_FILE} (or environment).")
    if not sps_password:
        raise ValueError(f"Missing SPS_PASSWORD in {_ENV_FILE} (or environment).")

    return Settings(
        rithum_url=rithum_url,
        rithum_username=rithum_username,
        rithum_password=rithum_password,
        sps_url=sps_url,
        sps_username=sps_username,
        sps_password=sps_password,
        headless=headless,
        timeout_ms=timeout_ms,
    )
